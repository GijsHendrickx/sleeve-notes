"""Sync a Discogs collection into the DB.

Two sources for the release listing:
  - Default: Discogs collection API (paginated, slow but always fresh)
  - --csv PATH: a Discogs CSV export (Collection → Export); per-release
                tracklists are still fetched from the API

The acting user is identified by DISCOGS_USER_ID. OAuth tokens come from
env (DISCOGS_OAUTH_TOKEN/_SECRET) if set; otherwise they're loaded from
the user's allauth SocialToken row. Consumer credentials come from env.
"""
from __future__ import annotations

import os
from pathlib import Path

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from records.services.discogs_sync import (
    make_oauth_session,
    sync_via_api,
    sync_via_csv,
)


User = get_user_model()


class Command(BaseCommand):
    help = "Sync a user's Discogs collection into the Release + Track tables."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv", type=str, default=None,
            help="Path to a Discogs CSV export. When set, release_ids come from "
                 "the CSV; per-release tracklists are still fetched from the API.",
        )
        parser.add_argument(
            "--folder", type=str, default=None,
            help="Discogs folder name or id. Default: whole collection (All).",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Stop after N releases (debug).",
        )
        parser.add_argument(
            "--username", type=str, default=None,
            help="Override the Discogs username for API-path sync. Default: "
                 "the value of DISCOGS_USERNAME env var.",
        )

    def handle(self, *args, **opts):
        user = self._require_user()
        token, token_secret = self._require_oauth_tokens(user)
        consumer_key, consumer_secret = self._require_consumer_credentials()
        session = make_oauth_session(consumer_key, consumer_secret, token, token_secret)

        log = self.stdout.write

        if opts["csv"]:
            csv_path = Path(opts["csv"]).expanduser().resolve()
            try:
                sync_via_csv(
                    user=user,
                    session=session,
                    csv_path=csv_path,
                    folder_filter=opts["folder"],
                    limit=opts["limit"],
                    log=log,
                )
            except (FileNotFoundError, ValueError) as e:
                raise CommandError(str(e))
            return

        username = opts["username"] or os.environ.get("DISCOGS_USERNAME") or user.username
        try:
            sync_via_api(
                user=user,
                session=session,
                username=username,
                folder=opts["folder"],
                limit=opts["limit"],
                log=log,
            )
        except ValueError as e:
            raise CommandError(str(e))

    def _require_user(self):
        raw = os.environ.get("DISCOGS_USER_ID")
        if not raw:
            raise CommandError(
                "DISCOGS_USER_ID must be set. Sign in via the web UI to "
                "populate it, or paste it into .env for standalone CLI use."
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

    def _require_oauth_tokens(self, user) -> tuple[str, str]:
        # Env wins (matches legacy JobRunner injection); fall back to allauth.
        env_token = os.environ.get("DISCOGS_OAUTH_TOKEN")
        env_secret = os.environ.get("DISCOGS_OAUTH_TOKEN_SECRET")
        if env_token and env_secret:
            return env_token, env_secret
        try:
            account = SocialAccount.objects.get(user=user, provider="discogs")
            token = SocialToken.objects.get(account=account)
        except (SocialAccount.DoesNotExist, SocialToken.DoesNotExist):
            raise CommandError(
                "No Discogs OAuth token found for this user. Sign in via the "
                "web UI, or paste DISCOGS_OAUTH_TOKEN / _SECRET into .env."
            )
        return token.token, token.token_secret

    def _require_consumer_credentials(self) -> tuple[str, str]:
        key = os.environ.get("DISCOGS_CONSUMER_KEY")
        secret = os.environ.get("DISCOGS_CONSUMER_SECRET")
        if not key or not secret:
            raise CommandError(
                "DISCOGS_CONSUMER_KEY and DISCOGS_CONSUMER_SECRET must be set."
            )
        return key, secret
