import uuid

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


class Feedback(TimestampedModel):
    """One message from a tester, plus just enough context to replay it.

    Deliberately no screenshot, form contents, query string or user agent:
    none of that is needed to find the screen, and all of it is data we'd
    then have to look after. Rows expire after FEEDBACK_RETENTION_DAYS
    (see services.prune_old_feedback).
    """

    MAX_LENGTH = 4000

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    message = models.TextField(max_length=MAX_LENGTH)
    path = models.CharField(max_length=500, blank=True)
    view_name = models.CharField(max_length=100, blank=True)
    # Plain values, not FKs: deleting the set shouldn't delete or orphan the
    # feedback about it.
    print_run_id = models.UUIDField(null=True, blank=True)
    step = models.IntegerField(null=True, blank=True)
    environment = models.CharField(max_length=20)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Feedback({self.created_at:%Y-%m-%d} {self.view_name or self.path})"
