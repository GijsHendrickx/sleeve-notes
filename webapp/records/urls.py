from django.urls import path

from records import action_views, views


app_name = "records"

urlpatterns = [
    path("collection", views.collection, name="collection"),
    path("collection/<uuid:release_id>", views.release_detail, name="release_detail"),
    path("tracks", views.tracks, name="tracks"),
    path("actions/discogs-sync", action_views.discogs_sync, name="action_discogs_sync"),
    path("actions/bpm", action_views.bpm_cascade, name="action_bpm_cascade"),
]
