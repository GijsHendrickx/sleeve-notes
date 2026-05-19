"""Run the 5-source BPM/key cascade for every track in a user's collection."""
from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from records.services.bpm_cascade import run_bpm_cascade


User = get_user_model()


class Command(BaseCommand):
    help = (
        "Look up BPM + key for every track in a user's collection using the "
        "5-source cascade. Idempotent — fully cached tracks are skipped."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workers", type=int, default=8,
            help="Concurrent tracks to cascade (default: 8). The per-host "
                 "rate limiter caps real throughput at the slowest source.",
        )

    def handle(self, *args, **opts):
        user = self._require_user()
        run_bpm_cascade(user, workers=opts["workers"], log=self.stdout.write)

    def _require_user(self):
        raw = os.environ.get("DISCOGS_USER_ID")
        if not raw:
            raise CommandError(
                "DISCOGS_USER_ID must be set. Sign in via the web UI to "
                "populate it, or paste it into .env for CLI use."
            )
        try:
            discogs_id = int(raw)
        except ValueError:
            raise CommandError(f"DISCOGS_USER_ID must be an integer, got {raw!r}")
        try:
            return User.objects.get(discogs_user_id=discogs_id)
        except User.DoesNotExist:
            raise CommandError(
                f"No Django user has discogs_user_id={discogs_id}. "
                "Sign in via the web UI first."
            )
