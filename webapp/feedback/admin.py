from django.contrib import admin

from feedback.models import Feedback


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "environment", "view_name", "step", "message")
    list_filter = ("environment", "view_name")
    readonly_fields = [f.name for f in Feedback._meta.fields]
    search_fields = ("message",)
