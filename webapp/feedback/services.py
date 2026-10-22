from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.urls import Resolver404, resolve
from django.utils import timezone

from feedback.models import Feedback


def page_context(current_url: str) -> dict:
    """Where the user was, from HTMX's HX-Current-URL header.

    Only the path survives; the query string can hold search terms we have
    no use for.
    """
    path = urlparse(current_url or "").path[:500]
    ctx = {"path": path, "view_name": "", "print_run_id": None, "step": None}
    try:
        match = resolve(path)
    except Resolver404:
        return ctx
    ctx["view_name"] = (match.view_name or "")[:100]
    ctx["print_run_id"] = match.kwargs.get("run_id")
    ctx["step"] = match.kwargs.get("step")
    return ctx


def create_feedback(user, message: str, current_url: str) -> Feedback:
    return Feedback.objects.create(
        user=user,
        message=message[: Feedback.MAX_LENGTH],
        environment=settings.ENVIRONMENT,
        **page_context(current_url),
    )


def prune_old_feedback() -> int:
    """Delete feedback past its retention. Runs daily as a Django-Q2 schedule."""
    cutoff = timezone.now() - timedelta(days=settings.FEEDBACK_RETENTION_DAYS)
    deleted, _ = Feedback.objects.filter(created_at__lt=cutoff).delete()
    return deleted
