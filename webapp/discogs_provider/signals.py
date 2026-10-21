from allauth.account.signals import user_logged_in
from django.dispatch import receiver

from discogs_provider.models import DiscogsConnection


@receiver(user_logged_in)
def store_discogs_token(sender, request, user, sociallogin=None, **kwargs):
    # allauth passes `sociallogin` on both first sign-up and returning logins,
    # so this is the one place a fresh Discogs token is ever in our hands.
    if sociallogin is None or sociallogin.account.provider != "discogs":
        return
    token = sociallogin.token
    if token is None or not token.token:
        return
    DiscogsConnection.objects.update_or_create(
        user=user,
        defaults={"token": token.token, "token_secret": token.token_secret},
    )
