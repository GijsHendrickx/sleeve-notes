from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import TemplateView


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("", TemplateView.as_view(template_name="landing.html"), name="landing"),
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    path("accounts/", include("allauth.urls")),
]
