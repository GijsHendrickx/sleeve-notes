"""Shared abstract model bases + small cross-cutting models."""
from django.conf import settings
from django.db import IntegrityError, models, transaction


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class LockHeld(Exception):
    """Raised when a user tries to start a job while one is already in flight."""

    def __init__(self, lock: "UserJobLock"):
        super().__init__(f"User {lock.user_id} already has an active {lock.kind} job.")
        self.lock = lock


class UserJobLock(models.Model):
    """At most one active background job per user, plus the just-finished one.

    Three states map to three toast states:
      running  — job in flight; toast shows live progress
      done     — job succeeded; toast shows result for ~5s, then vanishes
      failed   — job errored out; toast shows the error for ~5s

    A `done`/`failed` row is treated as "no active lock" by acquire() — a
    new click after completion replaces it cleanly. The toast view lazy-
    deletes done/failed rows older than COMPLETION_LINGER_S.
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="job_lock",
    )
    kind = models.CharField(max_length=50)
    task_id = models.CharField(max_length=128, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    # Latest progress line from the worker. The Q2 worker runs in a subprocess
    # and shares no memory with the web request — these columns are the bridge.
    progress_text = models.CharField(max_length=200, blank=True)
    progress_done = models.IntegerField(default=0)
    progress_total = models.IntegerField(default=0)
    # Completion state. `running` until the worker's finally clause flips it.
    state = models.CharField(max_length=20, default=STATE_RUNNING)
    finished_at = models.DateTimeField(null=True, blank=True)
    result_text = models.CharField(max_length=200, blank=True)

    # How long the toast keeps showing the completion message after the job
    # ends. The toast view lazy-deletes the row once this expires.
    COMPLETION_LINGER_S = 5

    def __str__(self):
        return f"{self.user_id}:{self.kind}:{self.state}"

    @classmethod
    def acquire(cls, user, kind: str, task_id: str = "") -> "UserJobLock":
        """Create the lock row. Raises LockHeld if a *running* lock exists.

        Done/failed locks from prior runs are replaced cleanly — a new
        click after completion always succeeds.
        """
        try:
            with transaction.atomic():
                return cls.objects.create(user=user, kind=kind, task_id=task_id)
        except IntegrityError:
            existing = cls.objects.get(user=user)
            if existing.state == cls.STATE_RUNNING:
                raise LockHeld(existing)
            # Done/failed leftover — replace it.
            existing.delete()
            with transaction.atomic():
                return cls.objects.create(user=user, kind=kind, task_id=task_id)

    @classmethod
    def mark_done(cls, user, result_text: str = "") -> None:
        from django.utils import timezone
        cls.objects.filter(user=user).update(
            state=cls.STATE_DONE,
            finished_at=timezone.now(),
            result_text=(result_text or "")[:200],
        )

    @classmethod
    def mark_failed(cls, user, error_text: str = "") -> None:
        from django.utils import timezone
        cls.objects.filter(user=user).update(
            state=cls.STATE_FAILED,
            finished_at=timezone.now(),
            result_text=(error_text or "")[:200],
        )

    @classmethod
    def release(cls, user) -> None:
        """Hard-delete the lock row. Use sparingly — `mark_done` is the
        normal path so the toast has time to show the completion message."""
        cls.objects.filter(user=user).delete()
