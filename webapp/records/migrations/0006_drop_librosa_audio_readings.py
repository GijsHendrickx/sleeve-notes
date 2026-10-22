"""Forget audio BPMs measured with librosa.

The audio engine changed to Essentia, and the benchmark that motivated it
had librosa wrong on two of three tracks. Dropping the reading and its
`sources_tried` mark makes cache_entry_complete() re-queue the track, so it
is measured again with Essentia when audio analysis is enabled.
"""
from django.db import migrations


AUDIO = "audio_analysis"


def forwards(apps, schema_editor):
    BpmCache = apps.get_model("records", "BpmCache")
    for row in BpmCache.objects.all().only("id", "sources_tried", "source_hits"):
        hits = dict(row.source_hits or {})
        tried = list(row.sources_tried or [])
        if AUDIO not in hits and AUDIO not in tried:
            continue
        hits.pop(AUDIO, None)
        row.source_hits = hits
        row.sources_tried = [s for s in tried if s != AUDIO]
        row.save(update_fields=["source_hits", "sources_tried"])


class Migration(migrations.Migration):

    dependencies = [("records", "0005_release_videos")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
