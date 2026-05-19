from allauth.socialaccount.providers.oauth.urls import default_urlpatterns

from .provider import DiscogsProvider


urlpatterns = default_urlpatterns(DiscogsProvider)
