from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from feedback.models import Feedback
from feedback.services import create_feedback


@login_required
@require_http_methods(["GET", "POST"])
def feedback(request):
    if not settings.FEEDBACK_ENABLED:
        raise Http404
    ctx = {
        "title": "Feedback",
        "max_length": Feedback.MAX_LENGTH,
        "retention_days": settings.FEEDBACK_RETENTION_DAYS,
    }
    if request.method == "GET":
        return render(request, "feedback/_modal.html", ctx)

    message = (request.POST.get("message") or "").strip()
    if not message:
        # 200, not 400: htmx doesn't swap error responses, so the message
        # would never reach the modal.
        ctx["error"] = "Write something first."
        return render(request, "feedback/_modal.html", ctx)
    create_feedback(request.user, message, request.headers.get("HX-Current-URL", ""))
    ctx["sent"] = True
    return render(request, "feedback/_modal.html", ctx)
