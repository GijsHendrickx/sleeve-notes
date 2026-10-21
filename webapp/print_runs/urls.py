from django.urls import path

from print_runs import flow, views


app_name = "print_runs"

urlpatterns = [
    path("", views.index, name="index"),
    path("new", flow.start, name="start"),
    path("<uuid:run_id>/step/<int:step>", flow.step, name="step"),
    path("<uuid:run_id>/bpm/status", flow.step2_status, name="step2_status"),
    path("<uuid:run_id>/bpm/lookup", flow.step2_lookup, name="step2_lookup"),
    path("<uuid:run_id>/design/preview", flow.step3_preview, name="step3_preview"),
    path("<uuid:run_id>", views.detail, name="detail"),
    path("<uuid:run_id>/delete", views.delete, name="delete"),
    path("<uuid:run_id>/duplicate", views.duplicate, name="duplicate"),
    path("<uuid:run_id>/pdf", views.pdf_download, name="pdf_download"),
]
