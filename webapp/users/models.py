from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    discogs_user_id = models.IntegerField(unique=True, null=True, blank=True)
    last_seen_at = models.DateTimeField(auto_now=True)
