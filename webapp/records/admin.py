from django.contrib import admin

from .models import Override, Release, Track


@admin.register(Release)
class ReleaseAdmin(admin.ModelAdmin):
    list_display = ("title", "discogs_release_id", "user", "year", "created_at")
    list_filter = ("user", "year")
    search_fields = ("title", "discogs_release_id")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ("position", "title", "release", "bpm", "bpm_resolved_at")
    list_filter = ("bpm_resolved_at",)
    search_fields = ("title", "release__title")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Override)
class OverrideAdmin(admin.ModelAdmin):
    list_display = ("track", "field", "user", "created_at")
    list_filter = ("field", "user")
    search_fields = ("track__title",)
    readonly_fields = ("id", "created_at", "updated_at")
