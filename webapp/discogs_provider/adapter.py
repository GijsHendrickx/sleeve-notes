"""Custom socialaccount adapter.

Sets a descriptive User-Agent on every OAuth HTTP request allauth makes.
Discogs rejects requests without User-Agent with HTTP 403 — the request_token
and access_token exchange calls happen inside allauth's OAuthClient, so we
have to inject the header at the session level rather than per call.
"""
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


USER_AGENT = "SleeveNotes/0.4 +https://github.com/GijsHendrickx/sleeve-notes"


class DiscogsSocialAccountAdapter(DefaultSocialAccountAdapter):
    def get_requests_session(self):
        session = super().get_requests_session()
        session.headers["User-Agent"] = USER_AGENT
        return session

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if sociallogin.account.provider == "discogs":
            try:
                user.discogs_user_id = int(sociallogin.account.uid)
            except (TypeError, ValueError):
                pass
        return user
