from django.contrib import admin

from .models import PrintRun


@admin.register(PrintRun)
class PrintRunAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "pdf_hash", "created_at")
    list_filter = ("user",)
    search_fields = ("name",)
    readonly_fields = ("id", "created_at", "updated_at", "pdf_hash")
