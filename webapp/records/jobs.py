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
import re
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


# `[done/total]` at the start of a progress line. Both the sync and the
# cascade emit this — we strip the brackets, swap for parens, and update
# UserJobLock.progress_done / _total so the toast can render a determinate
# bar with a percentage.
_PROGRESS_COUNTER_RE = re.compile(r"^\[(\d+)/(\d+)\]\s*")

# The BPM cascade appends confidence markers + the resolved BPM/key/sources
# after the artist — title. The user only wants to see the position +
# track name in the toast, so we strip both ends.
_CONFIDENCE_MARKERS_RE = re.compile(r"^[●○?≈]+\s*")
_BPM_SUFFIX_RE = re.compile(r"\s*->\s*\d+.*?sources=\[.*?\]\s*$")


def _prettify(msg: str) -> tuple[str, int, int]:
    """Clean a worker log line for display + extract (done, total) counters.

    Examples:
      "  [10/381] ●○ Madonna - Holiday -> 118 key=9B  sources=['beatport', ...]"
        → ("(10/381) Madonna - Holiday", 10, 381)

      "  page 2/2: 116 releases so far"
        → ("page 2/2: 116 releases so far", 0, 0)
    """
    trimmed = (msg or "").strip().lstrip("·-•").strip()
    if not trimmed:
        return "", 0, 0

    done = total = 0
    m = _PROGRESS_COUNTER_RE.match(trimmed)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        trimmed = trimmed[m.end():]
        trimmed = _CONFIDENCE_MARKERS_RE.sub("", trimmed)
        trimmed = _BPM_SUFFIX_RE.sub("", trimmed)
        trimmed = f"({done}/{total}) {trimmed}".rstrip()
    return trimmed[:200], done, total


def _make_progress_logger(user):
    """Return a log callable that mirrors lines to stdlib logging *and*
    persists progress to UserJobLock so the toast can render it. Each line
    is prettified (confidence markers stripped, [N/M] → (N/M), BPM suffix
    dropped) and the counters are extracted for the determinate progress bar.
    """
    def progress(msg: str) -> None:
        log.info(msg)
        text, done, total = _prettify(msg)
        if not text:
            return
        updates = {"progress_text": text}
        if total:
            updates["progress_done"] = done
            updates["progress_total"] = total
        UserJobLock.objects.filter(user=user).update(**updates)
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
        try:
            ck, cs, tok, tok_sec = _resolve_oauth(user)
            session = make_oauth_session(ck, cs, tok, tok_sec)
            if source == "csv":
                assert csv_path, "csv source requires csv_path"
                result = sync_via_csv(
                    user=user,
                    session=session,
                    csv_path=Path(csv_path),
                    folder_filter=folder,
                    limit=limit,
                    log=progress,
                )
            elif source == "api":
                uname = username or user.username
                result = sync_via_api(
                    user=user,
                    session=session,
                    username=uname,
                    folder=folder,
                    limit=limit,
                    log=progress,
                )
            else:
                raise ValueError(f"unknown sync source: {source!r}")
        except Exception as e:
            UserJobLock.mark_failed(user, f"Sync failed: {e}")
            raise
        UserJobLock.mark_done(
            user,
            f"Synced {len(result.get('release_ids', []))} releases "
            f"({result.get('fetched_this_run', 0)} new this run).",
        )
        return result
    finally:
        # Clean up any temp CSV upload so .tmp/uploads/ doesn't grow forever.
        if source == "csv" and csv_path:
            Path(csv_path).unlink(missing_ok=True)


def _run_bpm_cascade(user_id: int, *, workers: int = 8, force: bool = False) -> dict:
    user = User.objects.get(pk=user_id)
    progress = _make_progress_logger(user)
    try:
        result = run_bpm_cascade(user, workers=workers, force=force, log=progress)
    except Exception as e:
        UserJobLock.mark_failed(user, f"BPM lookup failed: {e}")
        raise
    found = result.get("found_bpm", 0)
    total = result.get("total_tracks", 0)
    cascaded = result.get("cascaded", 0)
    if total:
        UserJobLock.mark_done(
            user,
            f"Found BPM for {found} of {total} tracks "
            f"({cascaded} cascaded this run).",
        )
    else:
        UserJobLock.mark_done(user, result.get("error", "BPM lookup complete."))
    return result


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


def enqueue_bpm_cascade(user, *, workers: int = 8, force: bool = False) -> str:
    lock = UserJobLock.acquire(user, kind="bpm_cascade")
    try:
        task_id = async_task(
            _BPM_CASCADE_TASK,
            user.id,
            workers=workers,
            force=force,
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
