from django.urls import path

from records import views


app_name = "records"

urlpatterns = [
    path("collection", views.collection, name="collection"),
    path("collection/<uuid:release_id>", views.release_detail, name="release_detail"),
    path("tracks", views.tracks, name="tracks"),
]
