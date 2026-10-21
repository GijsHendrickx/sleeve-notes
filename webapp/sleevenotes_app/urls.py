from django.contrib import admin
from django.urls import include, path

from core import views as core_views


urlpatterns = [
    path("", core_views.landing, name="landing"),
    path("home", core_views.home, name="home"),
    path("", include("records.urls")),
    path("labels/", include("print_runs.urls")),
    path("admin/", admin.site.urls),
    path("healthz", core_views.healthz),
    path("run/banner", core_views.run_banner, name="run_banner"),
    path("accounts/", include("allauth.urls")),
]
