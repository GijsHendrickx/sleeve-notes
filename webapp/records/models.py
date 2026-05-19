import uuid

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


class Release(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    discogs_release_id = models.IntegerField()
    artist = models.CharField(max_length=500, blank=True)
    title = models.CharField(max_length=500)
    year = models.IntegerField(null=True, blank=True)
    compilation = models.BooleanField(default=False)
    labels = models.JSONField(null=True, blank=True)
    genres = models.JSONField(null=True, blank=True)
    styles = models.JSONField(null=True, blank=True)
    rpm = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    basic_information = models.JSONField(null=True, blank=True)
    raw_tracklist = models.JSONField(null=True, blank=True)
    release_type = models.CharField(max_length=50, blank=True)
    format = models.CharField(max_length=50, blank=True)
    thumb_url = models.URLField(max_length=500, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("user", "discogs_release_id")]
        indexes = [
            models.Index(fields=["user", "discogs_release_id"]),
            models.Index(fields=["user", "artist"]),
            models.Index(fields=["user", "release_type"]),
            models.Index(fields=["user", "format"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.discogs_release_id})"


class Track(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(Release, on_delete=models.CASCADE, related_name="tracks")
    position = models.CharField(max_length=20, blank=True)
    side = models.CharField(max_length=5, blank=True)
    artist = models.CharField(max_length=500, blank=True)
    title = models.CharField(max_length=500)
    duration = models.CharField(max_length=20, blank=True)
    duration_s = models.IntegerField(null=True, blank=True)
    spotify_track_id = models.CharField(max_length=64, blank=True)
    youtube_video_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["release", "position"]
        unique_together = [("release", "position")]

    def __str__(self):
        return f"{self.position} {self.title}".strip()


class BpmCache(TimestampedModel):
    """Per-(user, song) BPM/key cache.

    Keyed by a normalized artist+title hash so the same song on multiple
    releases (single + LP, single + compilation) shares a single cache row.
    The cascade writes here after consulting each source; the render layer
    reads from here to populate sticker BPMs.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    cache_key = models.CharField(max_length=64)
    artist = models.CharField(max_length=500)
    title = models.CharField(max_length=500)
    sources_tried = models.JSONField(default=list)
    source_hits = models.JSONField(default=dict)

    class Meta:
        unique_together = [("user", "cache_key")]
        indexes = [
            models.Index(fields=["user", "cache_key"]),
            models.Index(fields=["user", "artist", "title"]),
        ]
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.artist} — {self.title}"


class Override(TimestampedModel):
    """Manual BPM/key override.

    Either identify a track precisely (release + position) or broadly
    (artist + title). The broad form matches every track with that
    artist+title — useful for the same song on a single and an LP.
    Exactly one mode must be set; enforced at the application layer.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    release = models.ForeignKey(
        Release, on_delete=models.CASCADE, null=True, blank=True,
    )
    position = models.CharField(max_length=20, blank=True)
    artist = models.CharField(max_length=500, blank=True)
    title = models.CharField(max_length=500, blank=True)
    bpm = models.IntegerField(null=True, blank=True)
    key_camelot = models.CharField(max_length=20, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "release", "position"]),
            models.Index(fields=["user", "artist", "title"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        if self.release_id:
            return f"{self.release.title} @ {self.position}"
        return f"{self.artist} — {self.title}"
