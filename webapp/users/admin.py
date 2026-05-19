from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = (
        "username", "email", "discogs_user_id", "is_staff", "last_seen_at",
    )
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("username", "email", "discogs_user_id")
    readonly_fields = ("last_seen_at",)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Discogs", {"fields": ("discogs_user_id", "last_seen_at")}),
    )
