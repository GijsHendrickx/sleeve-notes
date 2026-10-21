"""Model fields that keep secrets out of the database in readable form."""
from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    # Newest key first: MultiFernet encrypts with keys[0] and tries every key
    # on decrypt, so rotating is "prepend the new key, re-save, drop the old".
    keys = [k.strip() for k in settings.FIELD_ENCRYPTION_KEY.split(",") if k.strip()]
    if not keys:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is empty. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"`."
        )
    return MultiFernet([Fernet(k) for k in keys])


class EncryptedTextField(models.TextField):
    """Fernet-encrypted at rest, plain `str` in Python.

    The ciphertext is randomised per write, so the column can't be filtered
    or indexed on — only read back whole.
    """

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return _fernet().decrypt(value.encode()).decode()

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None:
            return value
        return _fernet().encrypt(value.encode()).decode()

