"""Tag Sentry events with the user's id — and nothing else.

`send_default_pii` is off, so Sentry no longer attaches username, email or
IP on its own. The bare primary key is enough to trace an error back to a
tenant from our own database, without handing a name to a third party.
"""
import sentry_sdk


class SentryUserIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            sentry_sdk.set_user({"id": str(user.pk)})
        return self.get_response(request)
