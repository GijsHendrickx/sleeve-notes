from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth.provider import OAuthProvider


class DiscogsAccount(ProviderAccount):
    def to_str(self):
        return self.account.extra_data.get("username", super().to_str())


class DiscogsProvider(OAuthProvider):
    id = "discogs"
    name = "Discogs"
    account_class = DiscogsAccount

    def extract_uid(self, data):
        return str(data["id"])

    def extract_common_fields(self, data):
        return {"username": data.get("username", "")}


provider_classes = [DiscogsProvider]
