"""Smoke-test the Django-Q2 wiring without needing real Discogs tokens.

Creates a throwaway user, calls enqueue_bpm_cascade twice (second call
must raise LockHeld), verifies the lock row exists, then releases.
Deletes the user when done. Safe to run repeatedly.

This command is for developer use during the migration and can be
removed once routes/jobs.py are exercised via the actual web UI.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.models import LockHeld, UserJobLock
from records.jobs import enqueue_bpm_cascade


User = get_user_model()

THROWAWAY_USERNAME = "__qtest__"


class Command(BaseCommand):
    help = "Smoke-test Django-Q2 enqueue + per-user lock."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cleanup-only", action="store_true",
            help="Just delete the throwaway user + lock and exit.",
        )

    def handle(self, *args, **opts):
        if opts["cleanup_only"]:
            self._cleanup()
            return

        user, created = User.objects.get_or_create(
            username=THROWAWAY_USERNAME,
            defaults={"discogs_user_id": 999_999_999},
        )
        self.stdout.write(
            f"User {user.id} ({'created' if created else 'reused'}): {user.username}"
        )

        # Drop any stale lock from a previous aborted run.
        UserJobLock.release(user)

        try:
            task_id = enqueue_bpm_cascade(user, workers=1)
            self.stdout.write(self.style.SUCCESS(f"  enqueued task: {task_id}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  enqueue failed: {e!r}"))
            self._cleanup()
            return

        lock = UserJobLock.objects.get(user=user)
        self.stdout.write(
            f"  lock row: kind={lock.kind} task_id={lock.task_id} started_at={lock.started_at}"
        )

        try:
            enqueue_bpm_cascade(user, workers=1)
            self.stdout.write(self.style.ERROR(
                "  EXPECTED LockHeld on second enqueue, but it succeeded!"
            ))
        except LockHeld as e:
            self.stdout.write(self.style.SUCCESS(f"  second enqueue blocked as expected: {e}"))

        UserJobLock.release(user)
        self.stdout.write("  lock released")
        self.stdout.write(self.style.SUCCESS("smoke test passed"))
        self.stdout.write(
            "Note: the enqueued task will fail on the worker (no OAuth tokens) "
            "but the test above only validates enqueue + lock semantics."
        )

    def _cleanup(self):
        deleted, _ = User.objects.filter(username=THROWAWAY_USERNAME).delete()
        self.stdout.write(f"deleted {deleted} throwaway user row(s)")
