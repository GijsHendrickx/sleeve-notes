"""Move existing Discogs tokens out of allauth's plain-text SocialToken table.

Copying alone isn't enough: the plain rows would stay readable in every
future backup, which is the risk this table exists to remove.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    SocialToken = apps.get_model("socialaccount", "SocialToken")
    DiscogsConnection = apps.get_model("discogs_provider", "DiscogsConnection")
    tokens = SocialToken.objects.filter(account__provider="discogs").select_related("account")
    for t in tokens:
        DiscogsConnection.objects.update_or_create(
            user_id=t.account.user_id,
            defaults={"token": t.token, "token_secret": t.token_secret},
        )
    tokens.delete()


def backwards(apps, schema_editor):
    SocialAccount = apps.get_model("socialaccount", "SocialAccount")
    SocialToken = apps.get_model("socialaccount", "SocialToken")
    DiscogsConnection = apps.get_model("discogs_provider", "DiscogsConnection")
    for conn in DiscogsConnection.objects.all():
        account = SocialAccount.objects.filter(user_id=conn.user_id, provider="discogs").first()
        if account is None:
            continue
        SocialToken.objects.update_or_create(
            account=account,
            defaults={"token": conn.token, "token_secret": conn.token_secret, "app": None},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("discogs_provider", "0001_initial"),
        ("socialaccount", "0006_alter_socialaccount_extra_data"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
