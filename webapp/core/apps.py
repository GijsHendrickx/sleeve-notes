"""Core app config.

Hosts the dev-only in-process qcluster launcher. When the setting
``DEV_INPROCESS_QCLUSTER`` is True *and* we're inside a ``runserver``
process, we spin up a Django-Q2 cluster on a daemon thread so the
developer only needs one terminal. The setting defaults to False; the
worker service on Render keeps running ``manage.py qcluster`` in its own
process as usual.
"""
from __future__ import annotations

import os
import sys

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        self._maybe_start_inprocess_qcluster()

    @staticmethod
    def _maybe_start_inprocess_qcluster() -> None:
        import multiprocessing

        from django.conf import settings

        # Django-Q2 spawns worker child processes via multiprocessing. On macOS
        # those children re-import this module and re-run ready() — without
        # this guard we'd recursively try to start another cluster from inside
        # each worker, causing a circular-import crash.
        if multiprocessing.current_process().name != "MainProcess":
            return
        if not getattr(settings, "DEV_INPROCESS_QCLUSTER", False):
            return
        # Only when launched via `manage.py runserver`. We allow both:
        #   - autoreload: only the child (RUN_MAIN=true) gets the cluster
        #   - --noreload: runs in a single process
        is_runserver = "runserver" in sys.argv
        is_child_or_noreload = (
            os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv
        )
        if not (is_runserver and is_child_or_noreload):
            return

        import threading

        from django_q.cluster import Cluster

        thread = threading.Thread(
            target=Cluster().start,
            daemon=True,
            name="inproc-qcluster",
        )
        thread.start()
        sys.stdout.write(
            "[core] dev-only in-process qcluster started "
            "(set DEV_INPROCESS_QCLUSTER=false to disable)\n"
        )
