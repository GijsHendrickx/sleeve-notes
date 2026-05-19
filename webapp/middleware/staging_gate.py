"""HTTP Basic Auth gate for the staging environment.

Active only when ENVIRONMENT=staging. Requests to /healthz bypass the gate
so Render's health checker doesn't get a 401 and mark the service unhealthy.
"""
import base64
import secrets

from django.conf import settings
from django.http import HttpResponse


HEALTHCHECK_PATHS = {"/healthz"}


class StagingGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = settings.ENVIRONMENT == "staging"
        if self.enabled:
            self.username = settings.STAGING_BASIC_AUTH_USER
            self.password = settings.STAGING_BASIC_AUTH_PASSWORD

    def __call__(self, request):
        if not self.enabled or request.path in HEALTHCHECK_PATHS:
            return self.get_response(request)

        if self._authorized(request):
            return self.get_response(request)

        response = HttpResponse("Authentication required", status=401)
        response["WWW-Authenticate"] = f'Basic realm="{settings.APP_NAME} staging"'
        return response

    def _authorized(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return (
            secrets.compare_digest(user, self.username)
            and secrets.compare_digest(pwd, self.password)
        )
