"""Manage manual BPM/key overrides.

Two ways to address a track:
  precise: --release-id <Discogs id> --position <pos>
  broad:   --artist <name>           --title <title>
           (matches every track with that artist+title — same song on
           single + LP gets the same BPM/key in one row)

Usage:
  python manage.py overrides list
  python manage.py overrides add --release-id 123 --position A1 --bpm 128 --key 8A
  python manage.py overrides add --artist "Daft Punk" --title "One More Time" --bpm 123
  python manage.py overrides remove <UUID>
  python manage.py overrides clear --yes

The acting user is looked up by DISCOGS_USER_ID in the process env.
"""
from __future__ import annotations

import os
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from records.models import Override, Release


User = get_user_model()


class Command(BaseCommand):
    help = "Manage manual BPM/key overrides for the user identified by DISCOGS_USER_ID."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)

        sub.add_parser("list", help="List every override for the current user.")

        p_add = sub.add_parser("add", help="Add a new override.")
        p_add.add_argument("--release-id", type=int, default=None,
                           help="Discogs release id (use with --position).")
        p_add.add_argument("--position", type=str, default=None,
                           help="Track position, e.g. 'A1' (use with --release-id).")
        p_add.add_argument("--artist", type=str, default=None,
                           help="Track artist for broad match (use with --title).")
        p_add.add_argument("--title", type=str, default=None,
                           help="Track title for broad match (use with --artist).")
        p_add.add_argument("--bpm", type=int, default=None, help="Manual BPM.")
        p_add.add_argument("--key", type=str, default=None,
                           help="Musical key ('8A', 'Am', 'C# major', …).")
        p_add.add_argument("--note", type=str, default=None, help="Free-form note.")

        p_rm = sub.add_parser("remove", help="Remove an override by UUID.")
        p_rm.add_argument("id", type=str, help="Override UUID (see `overrides list`).")

        p_clear = sub.add_parser("clear", help="Remove every override (needs --yes).")
        p_clear.add_argument("--yes", action="store_true",
                             help="Confirm the destructive action.")

    def handle(self, *args, **options):
        user = self._require_user()
        action = options["action"]
        if action == "list":
            self._cmd_list(user)
        elif action == "add":
            self._cmd_add(user, options)
        elif action == "remove":
            self._cmd_remove(user, options)
        elif action == "clear":
            self._cmd_clear(user, options)

    def _require_user(self):
        raw = os.environ.get("DISCOGS_USER_ID")
        if not raw:
            raise CommandError(
                "DISCOGS_USER_ID must be set in env. Sign in via the web UI "
                "to populate it, or paste it into .env for standalone CLI use."
            )
        try:
            discogs_id = int(raw)
        except ValueError:
            raise CommandError(
                f"DISCOGS_USER_ID must be an integer, got {raw!r}"
            )
        try:
            return User.objects.get(discogs_user_id=discogs_id)
        except User.DoesNotExist:
            raise CommandError(
                f"No Django user has discogs_user_id={discogs_id}. "
                "Sign in via the web UI first."
            )

    def _cmd_list(self, user):
        rows = list(Override.objects.filter(user=user).order_by("created_at"))
        if not rows:
            self.stdout.write("(no overrides)")
            return
        for r in rows:
            bits: list[str] = []
            if r.release_id:
                bits.append(f"release_id={r.release.discogs_release_id} position={r.position!r}")
            else:
                bits.append(f"artist={r.artist!r} title={r.title!r}")
            if r.bpm is not None:
                bits.append(f"bpm={r.bpm}")
            if r.key_camelot:
                bits.append(f"key={r.key_camelot}")
            if r.note:
                bits.append(f"note={r.note!r}")
            self.stdout.write(f"{r.id}  " + "  ".join(bits))

    def _cmd_add(self, user, opts):
        has_rp = opts["release_id"] is not None and opts["position"] is not None
        has_at = opts["artist"] and opts["title"]
        if not has_rp and not has_at:
            raise CommandError(
                "overrides add needs either --release-id + --position, "
                "or --artist + --title."
            )
        if has_rp and has_at:
            raise CommandError(
                "overrides add: pass (--release-id + --position) "
                "OR (--artist + --title), not both."
            )
        if opts["bpm"] is None and not opts["key"]:
            raise CommandError("overrides add: pass at least one of --bpm, --key.")

        release = None
        if has_rp:
            try:
                release = Release.objects.get(
                    user=user, discogs_release_id=opts["release_id"],
                )
            except Release.DoesNotExist:
                raise CommandError(
                    f"No release with discogs_release_id={opts['release_id']} "
                    "for this user. Run `sleeve-notes fetch` first."
                )

        override = Override.objects.create(
            user=user,
            release=release,
            position=opts["position"] or "",
            artist=opts["artist"] or "",
            title=opts["title"] or "",
            bpm=opts["bpm"],
            key_camelot=opts["key"] or "",
            note=opts["note"] or "",
        )
        self.stdout.write(self.style.SUCCESS(f"Added override {override.id}."))

    def _cmd_remove(self, user, opts):
        try:
            uid = uuid.UUID(opts["id"])
        except ValueError:
            raise CommandError(f"Not a valid UUID: {opts['id']!r}")
        deleted, _ = Override.objects.filter(id=uid, user=user).delete()
        if not deleted:
            raise CommandError(f"No override {uid} for this user.")
        self.stdout.write(self.style.SUCCESS(f"Removed override {uid}."))

    def _cmd_clear(self, user, opts):
        if not opts["yes"]:
            raise CommandError(
                "Refusing to clear all overrides without --yes."
            )
        n, _ = Override.objects.filter(user=user).delete()
        self.stdout.write(self.style.SUCCESS(f"Removed {n} override(s)."))
