from django.conf import settings
from django.db import models

from core.models import TimestampedModel


class AuditEvent(TimestampedModel):
    """Append-only log of user actions.

    Foundation for the future OrderEvent model when paid orders land — both
    share this shape. Distinction at retention time: AuditEvent uses CASCADE
    (consistent with v1 hard-delete), OrderEvent will use SET_NULL because
    financial records must survive user-deletion.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True,
    )
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=50, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"{self.action} ({self.target_type}:{self.target_id})"
