"""Core app config.

Hosts the dev-only qcluster launcher. When ``DEV_INPROCESS_QCLUSTER`` is True
*and* we're inside a ``runserver`` process, we start a Django-Q2 cluster as a
child process so the developer only needs one terminal. The setting defaults
to False; the worker service on Render runs ``manage.py qcluster`` itself.
"""
from __future__ import annotations

import os
import sys

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        self._maybe_start_dev_qcluster()

    @staticmethod
    def _maybe_start_dev_qcluster() -> None:
        import multiprocessing

        from django.conf import settings

        # Django-Q2 spawns worker child processes via multiprocessing. Those
        # children re-import this module and re-run ready() — without this
        # guard we'd recursively try to start another cluster from inside
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

        import atexit
        import subprocess

        # The cluster MUST be its own process, not a thread in runserver.
        # django-q2 (1.11.1) hardcodes the "fork" start method whenever it's
        # available, and on macOS forking a parent that has threads running —
        # or that has already touched the network stack — kills the child the
        # moment it calls into CoreFoundation:
        #
        #   objc[…]: +[NSNumber initialize] may have been in progress in
        #   another thread when fork() was called. Crashing instead.
        #
        # requests hits that on its very first call, via macOS proxy lookup
        # (urllib's getproxies → SystemConfiguration). runserver is threaded,
        # so an in-process cluster forked workers that died instantly on every
        # job — the task never acked, its UserJobLock never released, and the
        # toast span forever. Forking from this clean child process is safe,
        # and it mirrors how the worker runs in production.
        manage_py = os.path.join(settings.BASE_DIR, "manage.py")
        env = {**os.environ, "DEV_INPROCESS_QCLUSTER": "false"}
        proc = subprocess.Popen([sys.executable, manage_py, "qcluster"], env=env)

        def _stop_cluster() -> None:
            if proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        atexit.register(_stop_cluster)
        sys.stdout.write(
            f"[core] dev qcluster started as pid {proc.pid} "
            "(set DEV_INPROCESS_QCLUSTER=false to disable)\n"
        )
