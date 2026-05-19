from allauth.socialaccount.providers.oauth.client import OAuth
from allauth.socialaccount.providers.oauth.views import (
    OAuthAdapter,
    OAuthCallbackView,
    OAuthLoginView,
)

from .provider import DiscogsProvider


REQUEST_TOKEN_URL = "https://api.discogs.com/oauth/request_token"
ACCESS_TOKEN_URL = "https://api.discogs.com/oauth/access_token"
AUTHORIZE_URL = "https://www.discogs.com/oauth/authorize"
IDENTITY_URL = "https://api.discogs.com/oauth/identity"


class DiscogsAPI(OAuth):
    def get_user_info(self):
        response = self.query(IDENTITY_URL, headers={"Accept": "application/json"})
        return response.json()


class DiscogsOAuthAdapter(OAuthAdapter):
    provider_id = DiscogsProvider.id
    request_token_url = REQUEST_TOKEN_URL
    access_token_url = ACCESS_TOKEN_URL
    authorize_url = AUTHORIZE_URL

    def complete_login(self, request, app, token, **kwargs):
        client = DiscogsAPI(request, app.client_id, app.secret, self.request_token_url)
        extra_data = client.get_user_info()
        return self.get_provider().sociallogin_from_response(request, extra_data)


oauth_login = OAuthLoginView.adapter_view(DiscogsOAuthAdapter)
oauth_callback = OAuthCallbackView.adapter_view(DiscogsOAuthAdapter)
