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
    """At most one active background job per user.

    The OneToOne to User is the single-slot enforcer at the DB layer. Acquire
    by creating the row inside a transaction; release by deleting it from the
    task's finally-block. If a worker is killed mid-task the row is stale —
    admin can delete it, or the user can wait for the auto-expiry helper.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="job_lock",
    )
    kind = models.CharField(max_length=50)
    task_id = models.CharField(max_length=128, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    # Latest progress line from the worker. The Q2 worker runs in a subprocess
    # and shares no memory with the web request — this column is the bridge.
    progress_text = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.user_id}:{self.kind}"

    @classmethod
    def acquire(cls, user, kind: str, task_id: str = "") -> "UserJobLock":
        """Create the lock row. Raises LockHeld if one already exists."""
        try:
            with transaction.atomic():
                return cls.objects.create(user=user, kind=kind, task_id=task_id)
        except IntegrityError:
            raise LockHeld(cls.objects.get(user=user))

    @classmethod
    def release(cls, user) -> None:
        cls.objects.filter(user=user).delete()
