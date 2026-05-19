from django.urls import path

from print_runs import views


app_name = "print_runs"

urlpatterns = [
    path("", views.index, name="index"),
    path("<uuid:run_id>", views.detail, name="detail"),
]
