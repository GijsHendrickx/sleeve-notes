from types import SimpleNamespace

from allauth.account.signals import user_logged_in
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings

from core.fields import _fernet
from discogs_provider.models import DiscogsConnection


User = get_user_model()


def _raw_columns(conn_id):
    with connection.cursor() as cur:
        cur.execute(
            "SELECT token, token_secret FROM discogs_provider_discogsconnection WHERE id = %s",
            [conn_id],
        )
        return cur.fetchone()


def _sociallogin(provider="discogs", token="tok", secret="sec"):
    return SimpleNamespace(
        account=SimpleNamespace(provider=provider),
        token=SimpleNamespace(token=token, token_secret=secret),
    )


class EncryptedTokenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="alice")

    def tearDown(self):
        _fernet.cache_clear()

    def test_round_trip_and_ciphertext_at_rest(self):
        conn = DiscogsConnection.objects.create(
            user=self.user, token="access-token", token_secret="access-secret"
        )
        raw_token, raw_secret = _raw_columns(conn.pk)
        self.assertNotIn("access-token", raw_token)
        self.assertNotIn("access-secret", raw_secret)

        conn.refresh_from_db()
        self.assertEqual(conn.token, "access-token")
        self.assertEqual(conn.token_secret, "access-secret")

    def test_old_key_still_decrypts_after_rotation(self):
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEY=old):
            _fernet.cache_clear()
            conn = DiscogsConnection.objects.create(user=self.user, token="t", token_secret="s")
        with override_settings(FIELD_ENCRYPTION_KEY=f"{new},{old}"):
            _fernet.cache_clear()
            conn.refresh_from_db()
            self.assertEqual(conn.token, "t")


class StoreTokenOnLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="alice")
        self.request = RequestFactory().get("/")

    def _login(self, sociallogin):
        user_logged_in.send(
            sender=User, request=self.request, user=self.user, sociallogin=sociallogin
        )

    def test_discogs_login_stores_and_refreshes_token(self):
        self._login(_sociallogin(token="first", secret="s1"))
        self._login(_sociallogin(token="second", secret="s2"))
        conn = DiscogsConnection.objects.get(user=self.user)
        self.assertEqual((conn.token, conn.token_secret), ("second", "s2"))

    def test_non_social_or_other_provider_login_is_ignored(self):
        self._login(None)
        self._login(_sociallogin(provider="github"))
        self.assertFalse(DiscogsConnection.objects.exists())
