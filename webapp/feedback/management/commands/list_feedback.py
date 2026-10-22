"""Print recent tester feedback, newest first — for a human or an agent.

Run locally against staging by pointing DATABASE_URL at the staging
database's external connection string.
"""
import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from feedback.models import Feedback


class Command(BaseCommand):
    help = "List tester feedback, newest first."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="How far back (default 30).")
        parser.add_argument("--json", action="store_true", dest="as_json", help="One JSON object per line.")

    def handle(self, *args, days, as_json, **options):
        since = timezone.now() - timedelta(days=days)
        rows = Feedback.objects.filter(created_at__gte=since)
        for fb in rows:
            record = {
                "id": str(fb.id),
                "created_at": fb.created_at.isoformat(timespec="seconds"),
                "user_id": fb.user_id,
                "environment": fb.environment,
                "path": fb.path,
                "view_name": fb.view_name,
                "print_run_id": str(fb.print_run_id) if fb.print_run_id else None,
                "step": fb.step,
                "message": fb.message,
            }
            if as_json:
                self.stdout.write(json.dumps(record, ensure_ascii=False))
            else:
                where = fb.view_name or fb.path or "?"
                if fb.step:
                    where += f" step {fb.step}"
                self.stdout.write(f"{record['created_at']}  user {fb.user_id}  {where}\n  {fb.message}\n")
        if not as_json:
            self.stdout.write(f"{rows.count()} item(s) in the last {days} days.")

