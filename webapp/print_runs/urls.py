from django.urls import path

from print_runs import views


app_name = "print_runs"

urlpatterns = [
    path("", views.index, name="index"),
    path("new", views.editor, name="editor"),
    path("<uuid:run_id>", views.detail, name="detail"),
    path("<uuid:run_id>/delete", views.delete, name="delete"),
    path("<uuid:run_id>/pdf", views.pdf_download, name="pdf_download"),
]
