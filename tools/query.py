"""Browse the bpm-stickers SQLite DB from the CLI.

Three modes:

  bpm-stickers query                        list every table with row counts
  bpm-stickers query <table> [--where ...]  dump rows from a table (read-only)
  bpm-stickers query --sql "SELECT ..."     run arbitrary read-only SQL

The `--sql` mode rejects statements that aren't a plain SELECT/WITH/PRAGMA/
EXPLAIN. Use the dedicated `overrides` subcommand for the only table the
user typically wants to mutate.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

try:
    from tools import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root

from tools import db as dbmod


_READONLY_PREFIX = re.compile(r"^\s*(SELECT|WITH|PRAGMA|EXPLAIN)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)
# Pragmas that mutate global state — keep PRAGMA read-only-ish.
_FORBIDDEN_PRAGMA = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|synchronous|foreign_keys|writable_schema)\s*=",
    re.IGNORECASE,
)


def _ensure_readonly(sql: str) -> None:
    if not _READONLY_PREFIX.match(sql):
        raise SystemExit("query --sql: only SELECT / WITH / PRAGMA / EXPLAIN are allowed.")
    if _FORBIDDEN.search(sql):
        raise SystemExit("query --sql: refusing mutating SQL (INSERT/UPDATE/DELETE/DROP/…).")
    if _FORBIDDEN_PRAGMA.match(sql):
        raise SystemExit("query --sql: refusing PRAGMA assignment.")


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_qident(table)})").fetchall()
    return [r["name"] for r in rows]


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _qident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise SystemExit(f"query: invalid identifier {name!r}")
    return f'"{name}"'


def _render_rows(rows: list, columns: list[str], as_json: bool) -> None:
    if as_json:
        out = [dict(zip(columns, [_jsonable(v) for v in r])) for r in rows]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return
    if not rows:
        print("(no rows)")
        return
    # Compute column widths from headers + cell contents.
    str_rows = [[_short(v) for v in r] for r in rows]
    widths = [len(c) for c in columns]
    for r in str_rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    # Cap each column to keep one row on one terminal line (best-effort).
    widths = [min(w, 64) for w in widths]
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    print(header)
    print("  ".join("-" * widths[i] for i in range(len(columns))))
    for r in str_rows:
        print("  ".join(_clip(cell, widths[i]).ljust(widths[i]) for i, cell in enumerate(r)))


def _short(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    if isinstance(v, str):
        return v.replace("\n", " ").replace("\t", " ")
    return str(v)


def _clip(s: str, w: int) -> str:
    if len(s) <= w:
        return s
    return s[: max(0, w - 1)] + "…"


def _jsonable(v):
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    return v


def _cmd_list_tables(conn: sqlite3.Connection, as_json: bool) -> int:
    tables = _list_tables(conn)
    rows = []
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {_qident(t)}").fetchone()["n"]
        rows.append((t, n))
    if as_json:
        out = [{"table": t, "rows": n} for t, n in rows]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    width = max((len(t) for t, _ in rows), default=5)
    for t, n in rows:
        print(f"{t.ljust(width)}  {n}")
    print()
    print(f"DB: {dbmod.db_path()}")
    print("Use `bpm-stickers query <table>` to dump a table, "
          "or `bpm-stickers query --sql \"SELECT …\"` for arbitrary SQL.")
    return 0


def _cmd_show_schema(conn: sqlite3.Connection, table: str | None) -> int:
    if table:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table','index') "
            "AND tbl_name = ? AND sql IS NOT NULL ORDER BY type DESC, name",
            (table,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table','index') "
            "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY tbl_name, type DESC, name"
        ).fetchall()
    if not rows:
        print(f"(no schema for {table!r})" if table else "(empty DB)")
        return 0
    for r in rows:
        print(r["sql"].rstrip() + ";")
        print()
    return 0


def _cmd_dump_table(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str] | None,
    where: str | None,
    order_by: str | None,
    limit: int,
    as_json: bool,
) -> int:
    available = _column_names(conn, table)
    if not available:
        raise SystemExit(f"query: table {table!r} not found.")
    if columns:
        for col in columns:
            if col not in available:
                raise SystemExit(
                    f"query: column {col!r} not in {table} "
                    f"(available: {', '.join(available)})"
                )
        select_cols = ", ".join(_qident(c) for c in columns)
        out_cols = columns
    else:
        select_cols = "*"
        out_cols = available
    sql = f"SELECT {select_cols} FROM {_qident(table)}"
    params: list = []
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        raise SystemExit(f"query: {e}")
    _render_rows([tuple(r) for r in rows], out_cols, as_json=as_json)
    if not as_json and limit and len(rows) == limit:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM {_qident(table)}"
            + (f" WHERE {where}" if where else "")
        ).fetchone()["n"]
        if total > limit:
            print(f"\n(showing {limit}/{total} rows — pass --limit 0 for all)")
    return 0


def _cmd_run_sql(conn: sqlite3.Connection, sql: str, as_json: bool) -> int:
    _ensure_readonly(sql)
    try:
        cur = conn.execute(sql)
    except sqlite3.Error as e:
        raise SystemExit(f"query: {e}")
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description] if cur.description else []
    _render_rows([tuple(r) for r in rows], columns, as_json=as_json)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bpm-stickers query",
        description="Browse the SQLite DB. Pass no args to list tables.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Table name to dump, or 'tables' / 'schema'. Omit to list tables.",
    )
    parser.add_argument(
        "--sql",
        type=str,
        default=None,
        help="Run an arbitrary read-only SQL statement (SELECT/WITH/PRAGMA/EXPLAIN).",
    )
    parser.add_argument(
        "--schema", action="store_true",
        help="Print CREATE statements for the named table, or all tables if no name given.",
    )
    parser.add_argument(
        "--where", type=str, default=None,
        help="WHERE clause (without the WHERE keyword), applied to the table dump.",
    )
    parser.add_argument(
        "--order-by", type=str, default=None,
        help="ORDER BY clause (without the keyword), applied to the table dump.",
    )
    parser.add_argument(
        "--cols", "--columns", dest="cols", type=str, default=None,
        help="Comma-separated subset of columns to show (table mode).",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Row limit for table dump / --sql (default 20; 0 = unlimited).",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit JSON instead of a text table.",
    )
    args = parser.parse_args(argv)

    if not dbmod.db_path().exists():
        # Open via dbmod.connect so the DB is created (and any legacy JSONs imported).
        with dbmod.session():
            pass

    with dbmod.session() as conn:
        if args.sql:
            return _cmd_run_sql(conn, args.sql, as_json=args.as_json)
        if args.schema:
            return _cmd_show_schema(conn, args.target)
        target = args.target
        if not target or target == "tables":
            return _cmd_list_tables(conn, as_json=args.as_json)
        if target == "schema":
            return _cmd_show_schema(conn, None)
        cols = [c.strip() for c in args.cols.split(",")] if args.cols else None
        return _cmd_dump_table(
            conn, target,
            columns=cols,
            where=args.where,
            order_by=args.order_by,
            limit=args.limit,
            as_json=args.as_json,
        )


if __name__ == "__main__":
    sys.exit(main())
