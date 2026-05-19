"""Render the sticker PDF for a user's collection.

Three modes, mutually exclusive:

  --print-run-id <uuid>   render a previously-saved print run
                          (stored releases + settings; layout flags ignored)
  --new-only              render only releases not yet in the print history
  (default)               render every release with tracks

The acting user is identified by DISCOGS_USER_ID. Output defaults to
.tmp/stickers.pdf at the repo root (same as the legacy CLI).
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from reportlab.lib.units import mm

from sleeve_notes import project_root
from sleeve_notes import sticker_layout as L

from print_runs.models import PrintRun
from print_runs.services.render import (
    DEFAULT_SETTINGS,
    already_printed_ids,
    create_print_run,
    get_print_run,
    last_print_timestamp,
    normalize_settings,
    releases_by_ids,
    releases_for_render,
    render_pdf,
)


User = get_user_model()

DEFAULT_PDF_OUT = project_root() / ".tmp" / "stickers.pdf"


class Command(BaseCommand):
    help = "Generate the printable A4 sticker PDF for a user's collection."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sticker-w", type=float, default=L.DEFAULT_STICKER_W_MM,
            help=f"Sticker width in mm (default: {L.DEFAULT_STICKER_W_MM}).",
        )
        parser.add_argument(
            "--sticker-h", type=float, default=L.DEFAULT_STICKER_H_MM,
            help=f"Sticker height in mm (default: {L.DEFAULT_STICKER_H_MM}).",
        )
        parser.add_argument(
            "--tile", action="store_true",
            help="Tile stickers edge-to-edge for ruler-cut sheets.",
        )
        parser.add_argument("--tile-cols", type=int, default=2)
        parser.add_argument("--tile-rows", type=int, default=5)
        parser.add_argument(
            "--new-only", action="store_true",
            help="Only render releases not yet in the print history.",
        )
        parser.add_argument(
            "--mark-printed", action="store_true",
            help="After rendering, save the rendered ids + settings as a new print run.",
        )
        parser.add_argument(
            "--print-run-id", type=str, default=None,
            help="UUID of a saved print run to re-render (settings + releases come "
                 "from the stored row; layout/selection flags are ignored).",
        )
        parser.add_argument("--no-qr", dest="qr", action="store_false")
        parser.add_argument("--no-artist", dest="show_artist", action="store_false")
        parser.add_argument("--no-title", dest="show_title", action="store_false")
        parser.add_argument("--no-rpm", dest="show_rpm", action="store_false")
        parser.add_argument("--no-key", dest="show_key", action="store_false")
        parser.add_argument("--no-bpm", dest="show_bpm", action="store_false")
        parser.add_argument("--no-duration", dest="show_duration", action="store_false")
        parser.add_argument("--no-track-title", dest="show_track_title", action="store_false")
        parser.add_argument("--no-sides", dest="show_sides", action="store_false")
        parser.set_defaults(**{k: v for k, v in DEFAULT_SETTINGS.items() if isinstance(v, bool)})
        parser.add_argument(
            "-o", "--output", type=str, default=str(DEFAULT_PDF_OUT),
            help=f"Output PDF path (default: {DEFAULT_PDF_OUT}).",
        )

    def handle(self, *args, **opts):
        if opts["print_run_id"] and (opts["new_only"] or opts["mark_printed"]):
            raise CommandError(
                "--print-run-id is mutually exclusive with --new-only / --mark-printed."
            )

        user = self._require_user()
        output_path = _resolve_output_path(opts["output"])

        run: PrintRun | None = None
        if opts["print_run_id"]:
            try:
                run_uuid = UUID(str(opts["print_run_id"]))
            except (TypeError, ValueError):
                raise CommandError(
                    f"--print-run-id must be a UUID, got {opts['print_run_id']!r}"
                )
            try:
                run = get_print_run(user, run_uuid)
            except PrintRun.DoesNotExist:
                raise CommandError(f"Print run {run_uuid} not found for this user.")
            settings = normalize_settings(run.settings)
            releases = releases_by_ids(user, [int(r) for r in run.release_ids])
            if not releases:
                raise CommandError(
                    f"Print run {run_uuid} has no renderable releases "
                    "(source releases removed or missing tracks)."
                )
        else:
            settings = {k: opts[k] for k in DEFAULT_SETTINGS if k in opts}
            releases = releases_for_render(user)
            if not releases:
                raise CommandError(
                    "No releases with tracks found. Run `sleeve-notes fetch` first."
                )
            if opts["new_only"]:
                already = already_printed_ids(user)
                before = len(releases)
                releases = [r for r in releases if int(r["id"]) not in already]
                skipped = before - len(releases)
                ts = last_print_timestamp(user)
                ts_str = f" (last print: {ts.isoformat(timespec='seconds')})" if ts else ""
                if not releases:
                    self.stdout.write(
                        f"Nothing new to print. All {before} release(s) are already "
                        f"in the print history{ts_str}."
                    )
                    return
                self.stdout.write(
                    f"--new-only: rendering {len(releases)} new release(s); "
                    f"skipping {skipped} already-printed{ts_str}."
                )

        try:
            result = render_pdf(
                user, releases=releases, settings=settings,
                output_path=output_path, log=self.stdout.write,
            )
        except ValueError as e:
            raise CommandError(str(e))

        layout = result.layout
        extra_note = (
            f" (incl. {result.extra_stickers} extra stickers from multi-disc / >8-track splits)"
            if result.extra_stickers else ""
        )
        layout_note = " edge-to-edge (tile mode)" if layout.tile_mode else ""
        self.stdout.write(
            f"Wrote {result.output_path}: {result.sticker_count} stickers from "
            f"{len(releases)} releases across {result.page_count} sticker page(s) "
            f"({layout.per_page}/page; {layout.sticker_w / mm:.1f}x{layout.sticker_h / mm:.1f} mm "
            f"on a {layout.cols}x{layout.rows} grid{layout_note}){extra_note}."
        )
        if layout.tile_mode:
            cuts = (layout.cols - 1) + (layout.rows - 1)
            self.stdout.write(
                f"  Tile mode: slice each page with {cuts} straight ruler cuts "
                f"({layout.cols - 1} vertical + {layout.rows - 1} horizontal). "
                "Print borderless or expect ~3 mm clipping on the outer stickers."
            )
        if result.missing_bpm_count:
            self.stdout.write(
                f"  {result.missing_bpm_count} tracks have an empty BPM box for handwriting."
            )

        if run is not None:
            run.pdf_hash = result.pdf_hash
            run.save(update_fields=["pdf_hash"])
            self.stdout.write(f"  Stamped print run {run.id} with pdf_hash={result.pdf_hash[:12]}…")
        elif opts["mark_printed"]:
            rendered_ids = [int(r["id"]) for r in releases]
            new_run = create_print_run(user, rendered_ids, settings)
            new_run.pdf_hash = result.pdf_hash
            new_run.save(update_fields=["pdf_hash"])
            total_in_history = len(already_printed_ids(user))
            self.stdout.write(
                f"  --mark-printed: saved as print run {new_run.id} "
                f"({len(rendered_ids)} release(s)). Total in print history: {total_in_history}."
            )

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


def _resolve_output_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if p.is_dir() or raw.endswith(("/", "\\")):
        p = p / "stickers.pdf"
    if p.suffix.lower() != ".pdf":
        p = p.with_suffix(".pdf")
    return p
