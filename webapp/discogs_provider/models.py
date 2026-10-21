from django.conf import settings
from django.db import models

from core.fields import EncryptedTextField
from core.models import TimestampedModel


class DiscogsConnection(TimestampedModel):
    """The user's Discogs access token + secret, encrypted at rest.

    Replaces allauth's `SocialToken`, which stores them in plain text
    (`SOCIALACCOUNT_STORE_TOKENS` is off). Written on every Discogs login by
    `signals.store_discogs_token`; read by background jobs that call the
    Discogs API on the user's behalf.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="discogs_connection",
    )
    token = EncryptedTextField()
    token_secret = EncryptedTextField()

    def __str__(self) -> str:
        return f"DiscogsConnection(user_id={self.user_id})"
