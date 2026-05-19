from django.contrib import admin

from .models import Override, Release, Track


@admin.register(Release)
class ReleaseAdmin(admin.ModelAdmin):
    list_display = (
        "title", "artist", "discogs_release_id", "year",
        "release_type", "format", "user", "created_at",
    )
    list_filter = ("user", "release_type", "format", "year")
    search_fields = ("title", "artist", "discogs_release_id")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ("position", "title", "artist", "release", "bpm", "bpm_resolved_at")
    list_filter = ("bpm_resolved_at",)
    search_fields = ("title", "artist", "release__title")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Override)
class OverrideAdmin(admin.ModelAdmin):
    list_display = (
        "user", "release", "position", "artist", "title",
        "bpm", "key_camelot", "created_at",
    )
    list_filter = ("user",)
    search_fields = ("artist", "title", "release__title")
    readonly_fields = ("id", "created_at", "updated_at")
