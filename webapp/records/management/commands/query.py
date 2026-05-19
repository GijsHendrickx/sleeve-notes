"""Minimal DB inspection — list models + counts, dump a model's first rows.

For anything richer (filtering, sorting, arbitrary SQL), use Django admin
at /admin/ or `python manage.py dbshell` / `python manage.py shell` directly.
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand


_INTERNAL_APPS = {
    "admin", "auth", "contenttypes", "sessions", "sites",
    "account", "socialaccount",
}


class Command(BaseCommand):
    help = (
        "Browse the DB. No args = list models + counts. "
        "Pass a model label (e.g. records.Release) to dump its first rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "model", nargs="?",
            help="Model label like 'records.Release'. Omit to list all models.",
        )
        parser.add_argument(
            "--limit", type=int, default=20,
            help="Rows to show (default 20; 0 = no limit).",
        )
        parser.add_argument(
            "--all-apps", action="store_true",
            help="Include Django internals (auth, sessions, admin, …) in the listing.",
        )

    def handle(self, *args, **opts):
        if not opts["model"]:
            self._list_models(opts["all_apps"])
            return
        self._dump_model(opts["model"], opts["limit"])

    def _list_models(self, all_apps):
        rows = []
        for app_config in apps.get_app_configs():
            if not all_apps and app_config.label in _INTERNAL_APPS:
                continue
            for model in app_config.get_models():
                rows.append((model._meta.label, model.objects.count()))
        rows.sort()
        width = max((len(r[0]) for r in rows), default=10)
        for label, count in rows:
            self.stdout.write(f"{label.ljust(width)}  {count}")
        self.stdout.write("")
        self.stdout.write("Tip: `python manage.py query <label>` dumps a model's first rows.")
        self.stdout.write("     `python manage.py dbshell` for arbitrary SQL.")

    def _dump_model(self, label, limit):
        try:
            model = apps.get_model(label)
        except LookupError:
            self.stderr.write(f"No such model: {label!r}.")
            return
        fields = [f.name for f in model._meta.concrete_fields]
        qs = model.objects.all()
        if limit > 0:
            qs = qs[:limit]
        rows = [
            [_short(getattr(obj, f, "")) for f in fields]
            for obj in qs
        ]
        if not rows:
            self.stdout.write("(no rows)")
            return
        widths = [len(f) for f in fields]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))
        widths = [min(w, 48) for w in widths]
        self.stdout.write("  ".join(f.ljust(widths[i]) for i, f in enumerate(fields)))
        self.stdout.write("  ".join("-" * widths[i] for i in range(len(fields))))
        for row in rows:
            self.stdout.write("  ".join(
                _clip(c, widths[i]).ljust(widths[i]) for i, c in enumerate(row)
            ))


def _short(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return s.replace("\n", " ").replace("\t", " ")


def _clip(s: str, w: int) -> str:
    if len(s) <= w:
        return s
    return s[: max(0, w - 1)] + "…"
