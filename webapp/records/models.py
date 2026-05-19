import uuid

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


class Release(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    discogs_release_id = models.IntegerField()
    title = models.CharField(max_length=500)
    artists = models.JSONField()
    raw_tracklist = models.JSONField(null=True, blank=True)
    year = models.IntegerField(null=True, blank=True)
    formats = models.JSONField(null=True, blank=True)
    labels = models.JSONField(null=True, blank=True)
    thumb_url = models.URLField(max_length=500, blank=True)

    class Meta:
        unique_together = [("user", "discogs_release_id")]
        indexes = [models.Index(fields=["user", "discogs_release_id"])]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.discogs_release_id})"


class Track(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(Release, on_delete=models.CASCADE, related_name="tracks")
    position = models.CharField(max_length=20, blank=True)
    title = models.CharField(max_length=500)
    duration = models.CharField(max_length=20, blank=True)
    bpm = models.FloatField(null=True, blank=True)
    bpm_sources_tried = models.JSONField(default=list)
    bpm_resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["release", "position"]

    def __str__(self):
        return f"{self.position} {self.title}".strip()


class Override(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="overrides")
    field = models.CharField(max_length=50)
    value = models.JSONField()

    class Meta:
        unique_together = [("user", "track", "field")]

    def __str__(self):
        return f"{self.track}: {self.field}"
