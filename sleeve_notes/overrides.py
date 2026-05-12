"""Manage manual BPM/key overrides stored in the DB.

Two ways to address a track:
  - precise:    --release-id <id> --position <pos>
  - broad:      --artist <name>   --title <title>   (matches every track
                with that artist+title via the same fuzzy-hash logic as
                the BPM cascade — useful for the same track appearing on
                a single + an LP)

Values:
  --bpm INT           manual BPM (50..250 enforced by the cascade)
  --key STR           musical key in any form ('8A', 'Am', 'C# major', …)
  --continuous-mix    flag the track as a continuous DJ-mix
                      (skips BPM lookup, shows "mix" on the sticker)
  --note STR          free-form note (just for your own reference)

Examples:
  sleeve-notes overrides add --release-id 123 --position A1 --bpm 128 --key 8A
  sleeve-notes overrides add --artist "Daft Punk" --title "Around the World" --bpm 121 --key Am
  sleeve-notes overrides add --release-id 789 --position A --continuous-mix --note "DJ mix"
  sleeve-notes overrides list
  sleeve-notes overrides remove 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from sleeve_notes import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sleeve_notes import project_root

from sleeve_notes import db as dbmod


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cmd_list(args) -> int:
    with dbmod.session() as conn:
        rows = conn.execute(
            "SELECT id, release_id, position, artist, title, bpm, key_camelot, "
            "continuous_mix, note, created_at FROM overrides ORDER BY id"
        ).fetchall()
    if not rows:
        print("(no overrides)")
        return 0
    width = max(len(str(r["id"])) for r in rows)
    for r in rows:
        bits: list[str] = []
        if r["release_id"] is not None:
            bits.append(f"release_id={r['release_id']} position={r['position']!r}")
        else:
            bits.append(f"artist={r['artist']!r} title={r['title']!r}")
        if r["continuous_mix"]:
            bits.append("continuous_mix=true")
        if r["bpm"] is not None:
            bits.append(f"bpm={r['bpm']}")
        if r["key_camelot"]:
            bits.append(f"key={r['key_camelot']}")
        if r["note"]:
            bits.append(f"note={r['note']!r}")
        print(f"#{str(r['id']).rjust(width)}  " + "  ".join(bits))
    return 0


def _cmd_add(args) -> int:
    has_rp = args.release_id is not None and args.position is not None
    has_at = args.artist and args.title
    if not has_rp and not has_at:
        print(
            "ERROR: overrides add needs either --release-id + --position, "
            "or --artist + --title.",
            file=sys.stderr,
        )
        return 2
    if has_rp and has_at:
        print(
            "ERROR: overrides add: pass either (--release-id + --position) "
            "OR (--artist + --title), not both.",
            file=sys.stderr,
        )
        return 2

    if (
        args.bpm is None
        and not args.key
        and not args.continuous_mix
    ):
        print(
            "ERROR: overrides add: pass at least one of --bpm, --key, --continuous-mix.",
            file=sys.stderr,
        )
        return 2

    with dbmod.session() as conn:
        cur = conn.execute(
            """
            INSERT INTO overrides (
                release_id, position, artist, title, bpm, key_camelot,
                continuous_mix, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.release_id,
                args.position,
                args.artist,
                args.title,
                args.bpm,
                args.key,
                1 if args.continuous_mix else 0,
                args.note,
                _now_iso(),
            ),
        )
        new_id = cur.lastrowid

    print(f"Added override #{new_id}.")
    return 0


def _cmd_remove(args) -> int:
    with dbmod.session() as conn:
        cur = conn.execute("DELETE FROM overrides WHERE id = ?", (args.id,))
        if cur.rowcount == 0:
            print(f"No override with id={args.id}.", file=sys.stderr)
            return 1
    print(f"Removed override #{args.id}.")
    return 0


def _cmd_clear(args) -> int:
    if not args.yes:
        print(
            "Refusing to clear all overrides without --yes. "
            "Run `sleeve-notes overrides clear --yes` to confirm.",
            file=sys.stderr,
        )
        return 2
    with dbmod.session() as conn:
        cur = conn.execute("DELETE FROM overrides")
        n = cur.rowcount
    print(f"Removed {n} override(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sleeve-notes overrides",
        description="Manage manual BPM/key overrides stored in the DB.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="List every override.")

    p_add = sub.add_parser("add", help="Add a new override.")
    p_add.add_argument("--release-id", type=int, default=None,
                       help="Discogs release id (use with --position).")
    p_add.add_argument("--position", type=str, default=None,
                       help="Track position, e.g. 'A1' (use with --release-id).")
    p_add.add_argument("--artist", type=str, default=None,
                       help="Track artist for broad match (use with --title).")
    p_add.add_argument("--title", type=str, default=None,
                       help="Track title for broad match (use with --artist).")
    p_add.add_argument("--bpm", type=int, default=None, help="Manual BPM (integer).")
    p_add.add_argument("--key", type=str, default=None,
                       help="Musical key ('8A', 'Am', 'C# major', 'F♯/G♭ Major', …).")
    p_add.add_argument("--continuous-mix", action="store_true",
                       help="Flag track as a continuous DJ-mix (no BPM lookup).")
    p_add.add_argument("--note", type=str, default=None, help="Free-form note.")

    p_rm = sub.add_parser("remove", help="Remove an override by id.")
    p_rm.add_argument("id", type=int, help="Override id (see `overrides list`).")

    p_clear = sub.add_parser("clear", help="Remove every override (needs --yes).")
    p_clear.add_argument("--yes", action="store_true",
                         help="Confirm the destructive action.")

    args = parser.parse_args(argv)
    if args.action == "list":
        return _cmd_list(args)
    if args.action == "add":
        return _cmd_add(args)
    if args.action == "remove":
        return _cmd_remove(args)
    if args.action == "clear":
        return _cmd_clear(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
