"""Housekeeping that nothing was doing: expiring drafts, and forgetting people.

Two gaps, same shape - a rule the code stated and never enforced.

  **Drafts never expired.** `expires_at` was written on every register and shown
  on the oversight page as "expiring", and SESSION_TTL_HOURS was defined, and
  nothing anywhere acted on any of it. Yesterday's half-finished register stayed
  open indefinitely, and the "still draft" count drifted upward forever.

  **Nothing was ever forgotten.** An abandoned signup left a person row, a face
  template and a phone number permanently; so did a rejected one. Most of the
  people involved are minors. A face you keep for someone you decided not to
  enrol is the hardest kind of data to justify holding.

WHY NOT A SCHEDULER
-------------------
There is no scheduler in this deployment and adding one - a worker, a cron
container, a beat process - is a second thing to deploy and a second thing to
notice has died. These sweeps are cheap and idempotent, so they run from the
paths that already happen: startup, opening a register, and the oversight page.
`run_due` rate-limits itself so a busy centre does not sweep on every request.

Running per worker means it may run twice as often with two workers. Both are
idempotent DELETEs, so the second finds nothing.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Dict

from . import config
from .db import connect

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 15 * 60

_last_run = 0.0
_lock = threading.Lock()


def _cutoff(days: int) -> str:
    return (config.local_now() - timedelta(days=days)) \
        .replace(tzinfo=None).isoformat(timespec="seconds")


def expire_drafts() -> int:
    """Close registers nobody submitted. Returns how many.

    The drafts inside them go with the session: a draft is a proposal, and an
    18-hour-old proposal nobody signed is not attendance and must never become
    it. Confirmed rows are untouched by construction - the WHERE only ever
    matches sessions still in 'draft'.
    """
    now = config.local_now().replace(tzinfo=None).isoformat(timespec="seconds")
    with connect() as conn:
        stale = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM attendance_sessions WHERE status = 'draft' AND expires_at < ?",
            (now,)).fetchall()]
        if not stale:
            return 0
        marks = ",".join("?" for _ in stale)
        conn.execute(
            f"DELETE FROM attendance WHERE status = 'draft' AND session_id IN ({marks})",
            stale)
        conn.execute(
            f"UPDATE attendance_sessions SET status = 'expired' WHERE id IN ({marks})",
            stale)
    log.info("Expired %d register(s) nobody submitted.", len(stale))
    return len(stale)


def purge_abandoned_signups() -> Dict[str, int]:
    """Forget applications that were never decided, and ones that were refused.

    Deletes the person, their face templates and their account together. Half a
    deletion is worse than none: a students row with no account is a name nobody
    can explain, and templates with no person are a face the gallery cannot
    name.

    Deliberately narrow. It only ever touches people whose record was created BY
    a signup and never approved - never somebody an admin enrolled, and never
    anybody who has attendance against their name.
    """
    out = {"pending": 0, "rejected": 0}
    pend_before = _cutoff(config.PENDING_SIGNUP_TTL_DAYS)
    rej_before = _cutoff(config.REJECTED_SIGNUP_TTL_DAYS)

    with connect() as conn:
        rows = conn.execute(
            "SELECT u.id AS user_id, u.student_id, u.status FROM users u "
            "WHERE (u.status = 'pending'  AND u.created_at  < ?) "
            "   OR (u.status = 'rejected' AND COALESCE(u.approved_at, u.created_at) < ?)",
            (pend_before, rej_before),
        ).fetchall()
        for r in rows:
            sid = r["student_id"]
            if sid is not None:
                # An unapproved person should have no attendance at all - the
                # gallery excludes them and both writes refuse them. If one does,
                # something wrote it that should not have, and deleting the
                # person would take the evidence with it. Leave it and say so.
                n = conn.execute(
                    "SELECT COUNT(*) FROM attendance WHERE student_id = ?", (sid,)
                ).fetchone()[0]
                if n:
                    log.warning("Not purging person %s: they have %d attendance row(s) "
                                "despite never being approved. Investigate.", sid, n)
                    continue
                conn.execute("DELETE FROM coach_athletes WHERE coach_id = ? OR athlete_id = ?",
                             (sid, sid))
                conn.execute("DELETE FROM templates WHERE student_id = ?", (sid,))
            conn.execute("DELETE FROM signup_tokens WHERE user_id = ?", (r["user_id"],))
            conn.execute("DELETE FROM users WHERE id = ?", (r["user_id"],))
            if sid is not None:
                conn.execute("DELETE FROM students WHERE id = ?", (sid,))
            out[r["status"]] = out.get(r["status"], 0) + 1

    if out["pending"] or out["rejected"]:
        log.warning("Retention: forgot %d abandoned and %d refused registration(s), "
                    "including their face templates.", out["pending"], out["rejected"])
    return out


def run_all() -> Dict[str, int]:
    """Every sweep, unconditionally. Safe to call at any time."""
    drafts = expire_drafts()
    purged = purge_abandoned_signups()
    return {"expired_registers": drafts, "purged_pending": purged["pending"],
            "purged_rejected": purged["rejected"]}


def run_due(force: bool = False) -> Dict[str, int]:
    """Run the sweeps if they are due. Cheap to call from a hot path.

    Failures are logged, never raised: housekeeping must not be able to break
    the request that happened to trigger it.
    """
    global _last_run
    now = time.time()
    with _lock:
        if not force and now - _last_run < INTERVAL_SECONDS:
            return {}
        _last_run = now
    try:
        return run_all()
    except Exception as e:                # noqa: BLE001
        log.warning("Maintenance sweep failed (will retry later): %s", e)
        return {}
