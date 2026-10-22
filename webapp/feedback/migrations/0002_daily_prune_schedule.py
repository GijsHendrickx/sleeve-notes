"""Register the daily retention job, so expiry doesn't depend on someone
remembering to run a command."""
from django.db import migrations


SCHEDULE_NAME = "feedback.prune_old_feedback"


def forwards(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=SCHEDULE_NAME,
        defaults={"func": "feedback.services.prune_old_feedback", "schedule_type": "D"},
    )


def backwards(apps, schema_editor):
    apps.get_model("django_q", "Schedule").objects.filter(name=SCHEDULE_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("feedback", "0001_initial"),
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
