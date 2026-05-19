from django.urls import path

from records import views


app_name = "records"

urlpatterns = [
    path("collection", views.collection, name="collection"),
    path("tracks", views.tracks, name="tracks"),
]
