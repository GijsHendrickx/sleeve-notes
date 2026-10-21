from django.apps import AppConfig


class DiscogsProviderConfig(AppConfig):
    name = "discogs_provider"
    verbose_name = "Discogs OAuth provider"

    def ready(self):
        from discogs_provider import signals  # noqa: F401
