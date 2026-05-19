"""Background jobs for records (Discogs sync, BPM cascade).

Two responsibilities live here:

1. The `_run_*` functions that Django-Q2 actually executes on workers.
   They re-resolve the User from id (workers don't share request state),
   wire up OAuth, run the service-layer function, and release the lock
   in a finally-block so a crash still frees the user's slot.

2. The `enqueue_*` helpers used by routes / management commands. They
   acquire the lock first, then hand off to `async_task`. If the lock
   is held, they raise LockHeld and never enqueue.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth import get_user_model
from django_q.tasks import async_task

from core.models import LockHeld, UserJobLock

from records.services.bpm_cascade import run_bpm_cascade
from records.services.discogs_sync import (
    make_oauth_session,
    sync_via_api,
    sync_via_csv,
)


User = get_user_model()
log = logging.getLogger(__name__)


def _make_progress_logger(user):
    """Return a log callable that mirrors lines to stdlib logging *and*
    persists the latest line to UserJobLock.progress_text so the toast
    can read it. Each line is trimmed + truncated to the column limit.
    """
    def progress(msg: str) -> None:
        log.info(msg)
        trimmed = (msg or "").strip().lstrip("·-•").strip()
        if not trimmed:
            return
        UserJobLock.objects.filter(user=user).update(
            progress_text=trimmed[:200],
        )
    return progress


# ─── Credential resolution ───────────────────────────────────────────────────


def _resolve_oauth(user) -> tuple[str, str, str, str]:
    """Return (consumer_key, consumer_secret, access_token, access_secret).

    Consumer creds always come from env (one app-wide pair). Access tokens
    come from the user's allauth SocialToken row — workers have no HTTP
    session, so env-injected tokens (legacy JobRunner pattern) don't apply.
    """
    consumer_key = os.environ.get("DISCOGS_CONSUMER_KEY", "")
    consumer_secret = os.environ.get("DISCOGS_CONSUMER_SECRET", "")
    if not consumer_key or not consumer_secret:
        raise RuntimeError("DISCOGS_CONSUMER_KEY / _SECRET must be set in env.")
    account = SocialAccount.objects.get(user=user, provider="discogs")
    token = SocialToken.objects.get(account=account)
    return consumer_key, consumer_secret, token.token, token.token_secret


# ─── Worker entrypoints ──────────────────────────────────────────────────────


def _run_discogs_sync(
    user_id: int,
    *,
    source: str,
    csv_path: str | None = None,
    folder: str | None = None,
    username: str | None = None,
    limit: int | None = None,
) -> dict:
    """Worker-side runner. Q2 invokes this with the args from enqueue_*."""
    user = User.objects.get(pk=user_id)
    progress = _make_progress_logger(user)
    try:
        ck, cs, tok, tok_sec = _resolve_oauth(user)
        session = make_oauth_session(ck, cs, tok, tok_sec)
        if source == "csv":
            assert csv_path, "csv source requires csv_path"
            return sync_via_csv(
                user=user,
                session=session,
                csv_path=Path(csv_path),
                folder_filter=folder,
                limit=limit,
                log=progress,
            )
        if source == "api":
            uname = username or user.username
            return sync_via_api(
                user=user,
                session=session,
                username=uname,
                folder=folder,
                limit=limit,
                log=progress,
            )
        raise ValueError(f"unknown sync source: {source!r}")
    finally:
        UserJobLock.release(user)


def _run_bpm_cascade(user_id: int, *, workers: int = 8) -> dict:
    user = User.objects.get(pk=user_id)
    progress = _make_progress_logger(user)
    try:
        return run_bpm_cascade(user, workers=workers, log=progress)
    finally:
        UserJobLock.release(user)


# ─── Enqueue helpers (called from routes / commands) ─────────────────────────


_DISCOGS_SYNC_TASK = "records.jobs._run_discogs_sync"
_BPM_CASCADE_TASK = "records.jobs._run_bpm_cascade"


def enqueue_discogs_sync(
    user,
    *,
    source: str,
    csv_path: str | None = None,
    folder: str | None = None,
    username: str | None = None,
    limit: int | None = None,
) -> str:
    """Acquire the lock and enqueue a sync. Returns the Q2 task id.

    Raises LockHeld if the user already has an active job.
    """
    if source not in ("csv", "api"):
        raise ValueError(f"sync source must be 'csv' or 'api', got {source!r}")
    lock = UserJobLock.acquire(user, kind="discogs_sync")
    try:
        task_id = async_task(
            _DISCOGS_SYNC_TASK,
            user.id,
            source=source,
            csv_path=csv_path,
            folder=folder,
            username=username,
            limit=limit,
            task_name=f"discogs_sync:{user.id}",
        )
    except Exception:
        UserJobLock.release(user)
        raise
    lock.task_id = task_id
    lock.save(update_fields=["task_id"])
    return task_id


def enqueue_bpm_cascade(user, *, workers: int = 8) -> str:
    lock = UserJobLock.acquire(user, kind="bpm_cascade")
    try:
        task_id = async_task(
            _BPM_CASCADE_TASK,
            user.id,
            workers=workers,
            task_name=f"bpm_cascade:{user.id}",
        )
    except Exception:
        UserJobLock.release(user)
        raise
    lock.task_id = task_id
    lock.save(update_fields=["task_id"])
    return task_id


__all__: Iterable[str] = (
    "LockHeld",
    "enqueue_bpm_cascade",
    "enqueue_discogs_sync",
)
