import uuid

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


class PrintRun(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=200, blank=True)
    settings = models.JSONField()
    release_ids = models.JSONField()
    pdf_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"Print run {self.id}"
