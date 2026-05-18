"""Background job runner for the web app's Run panel.

A job is one subprocess invocation of ``sleeve-notes <subcommand> [args]``.
Stdout/stderr are merged, captured line-by-line, and broadcast to every
connected SSE subscriber. At most one job runs at a time — a second
``start()`` while one is active returns the existing job rather than
queueing.

The runner is plain threading + ``asyncio.Queue`` per subscriber. The
subprocess prints flush on newlines because we set ``PYTHONUNBUFFERED=1``
in its environment.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal


JobKind = Literal["fetch", "bpm", "render", "run"]
JobStatus = Literal["running", "done", "failed", "cancelled"]


@dataclass
class JobEvent:
    """One line of subprocess output, or a status transition."""
    kind: Literal["line", "status"]
    text: str = ""           # for kind=line
    status: str = ""         # for kind=status
    seq: int = 0


@dataclass
class Job:
    id: str
    kind: JobKind
    argv: list[str]
    started_at: float
    user_id: int
    username: str
    status: JobStatus = "running"
    finished_at: float | None = None
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None


class JobRunner:
    """Singleton runner. One job at a time."""

    def __init__(self) -> None:
        self._current: Job | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue[JobEvent]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the FastAPI loop so worker threads can push into queues."""
        self._loop = loop

    @property
    def current(self) -> Job | None:
        return self._current

    def subscribe(self) -> asyncio.Queue[JobEvent]:
        q: asyncio.Queue[JobEvent] = asyncio.Queue()
        self._subscribers.append(q)
        # Replay buffered history so a late subscriber sees the whole run.
        if self._current is not None:
            for line in list(self._current.lines):
                self._seq += 1
                q.put_nowait(JobEvent(kind="line", text=line, seq=self._seq))
            if self._current.status != "running":
                self._seq += 1
                q.put_nowait(JobEvent(kind="status", status=self._current.status, seq=self._seq))
        return q

    def unsubscribe(self, q: asyncio.Queue[JobEvent]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, event: JobEvent) -> None:
        loop = self._loop
        if loop is None:
            return
        for q in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                # Loop closed during shutdown.
                pass

    def start(
        self,
        kind: JobKind,
        argv: list[str],
        *,
        user_id: int,
        username: str,
        discogs_token: str,
        discogs_token_secret: str,
    ) -> tuple[Job, bool]:
        """Start a job. Returns (job, started_now). If a job is running, returns
        it with started_now=False.

        The four ``discogs_*`` args are injected as env vars into the
        subprocess so the per-user OAuth signing in
        ``fetch_discogs_collection`` works. Tokens are NOT stored on the
        Job — only on the worker thread's stack — so they disappear once
        the subprocess exits. One job at a time globally; a second user's
        start() while one is in flight returns the existing job.
        """
        with self._lock:
            if self._current is not None and self._current.status == "running":
                return self._current, False
            job = Job(
                id=uuid.uuid4().hex[:8],
                kind=kind,
                argv=list(argv),
                started_at=time.time(),
                user_id=user_id,
                username=username,
            )
            self._current = job
            self._seq = 0
        # Tell any existing subscribers we're starting fresh.
        self._broadcast(JobEvent(kind="status", status="running", seq=0))
        t = threading.Thread(
            target=self._run,
            args=(job, discogs_token, discogs_token_secret),
            daemon=True,
        )
        t.start()
        return job, True

    def cancel(self) -> bool:
        with self._lock:
            if self._current is None or self._current.status != "running":
                return False
            if self._proc is None:
                return False
            proc = self._proc
        try:
            proc.terminate()
        except ProcessLookupError:
            return False
        # SIGTERM-only is not enough — sleeve-notes bpm has non-daemon worker
        # threads that can block subprocess exit when stuck in network I/O.
        # Escalate to SIGKILL after a short grace period via a background
        # watchdog so cancel() returns immediately to the request handler.
        def _watchdog():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        threading.Thread(target=_watchdog, daemon=True).start()
        return True

    def _run(self, job: Job, discogs_token: str, discogs_token_secret: str) -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Strip the legacy single-tenant Discogs vars in case they're in the
        # parent process — they'd silently override the per-user OAuth.
        env.pop("DISCOGS_TOKEN", None)
        env["DISCOGS_USER_ID"] = str(job.user_id)
        env["DISCOGS_USERNAME"] = job.username
        env["DISCOGS_OAUTH_TOKEN"] = discogs_token
        env["DISCOGS_OAUTH_TOKEN_SECRET"] = discogs_token_secret
        cmd = [sys.executable, "-m", "sleeve_notes.cli", job.kind, *job.argv]
        self._append_line(job, f"$ {' '.join(cmd)}")
        try:
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                env=env,
            ) as proc:
                self._proc = proc
                assert proc.stdout is not None
                for raw in proc.stdout:
                    self._append_line(job, raw.rstrip("\n"))
                proc.wait()
                job.exit_code = proc.returncode
                if proc.returncode == 0:
                    job.status = "done"
                elif proc.returncode in (-15, -9, 143, 137):
                    # SIGTERM=-15/143, SIGKILL=-9/137. Both mean user-cancel
                    # (the watchdog in cancel() escalates if SIGTERM stalls).
                    job.status = "cancelled"
                else:
                    job.status = "failed"
                    self._append_line(job, f"[exited with code {proc.returncode}]")
        except Exception as e:
            job.status = "failed"
            self._append_line(job, f"[runner error: {e}]")
        finally:
            job.finished_at = time.time()
            self._proc = None
            self._seq += 1
            self._broadcast(JobEvent(kind="status", status=job.status, seq=self._seq))

    def _append_line(self, job: Job, line: str) -> None:
        job.lines.append(line)
        self._seq += 1
        self._broadcast(JobEvent(kind="line", text=line, seq=self._seq))


runner = JobRunner()
