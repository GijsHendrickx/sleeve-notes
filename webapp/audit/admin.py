from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "target_type", "target_id", "created_at")
    list_filter = ("action", "target_type", "user")
    search_fields = ("action", "target_id")
    readonly_fields = ("id", "created_at", "updated_at")
