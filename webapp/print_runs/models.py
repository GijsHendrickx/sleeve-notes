import uuid

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


class PrintRun(TimestampedModel):
    """One set of sleeve labels.

    A run starts life as a `draft` the moment the user enters the flow, so
    closing the tab mid-way never loses the selection. `step` records how far
    they got; the flow reopens there. Only a `saved` run counts as printed —
    `already_printed_ids` and the "new since last print" badge both ignore
    drafts, otherwise abandoning a draft would mark records as done.
    """

    STATUS_DRAFT = "draft"
    STATUS_SAVED = "saved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=200, blank=True)
    settings = models.JSONField()
    release_ids = models.JSONField()
    pdf_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, default=STATUS_SAVED)
    step = models.IntegerField(default=1)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"Sleeve labels {self.id}"

    @property
    def is_draft(self) -> bool:
        return self.status == self.STATUS_DRAFT

    @property
    def display_name(self) -> str:
        return self.name or "Untitled set"
