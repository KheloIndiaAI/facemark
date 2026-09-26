"""Forgotten passwords, approved by a super admin.

Somebody who cannot sign in asks for a new password from the sign-in screen:
their username and the password they want. Nothing changes yet. A super admin
sees the request under Accounts, checks with the person that it is really them,
and approves it - only then does the new password replace the old one.

The design follows from who is asking. The request is unauthenticated, so:

  * The reply is the same whether or not the username exists, is active, or
    already has a request waiting. Anything else is a way to list accounts.
  * The address is throttled BEFORE the password is hashed - hashing is 600,000
    PBKDF2 rounds, and a guard that hashed first would burn the CPU it guards.
  * The new password is stored hashed, never in plain text, and is never shown
    to the admin. The admin approves a person, not a password.
  * The old password keeps working until approval. A stranger asking for a
    reset on somebody else's account cannot lock them out by asking.
  * Approving signs the account out everywhere and clears any sign-in lock, the
    same as an admin reset.

A request older than PASSWORD_RESET_TTL_DAYS cannot be approved: by then nobody
can ask the person whether they still want it.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

from . import auth, config, database

log = logging.getLogger("password_reset")

# Per-address request times. In-process, like the login throttle - see
# config.LOGIN_IP_WINDOW_SECONDS for why that trade is accepted.
_ip_requests: dict = {}
_ip_lock = threading.Lock()


class ResetThrottled(Exception):
    pass


class ResetNotFound(Exception):
    pass


class ResetNotPending(Exception):
    pass


def _throttled(ip: str) -> bool:
    if not ip:
        return False
    now = time.time()
    cutoff = now - config.PASSWORD_RESET_IP_WINDOW_SECONDS
    with _ip_lock:
        seen = [t for t in _ip_requests.get(ip, ()) if t > cutoff]
        seen.append(now)
        _ip_requests[ip] = seen
        if len(_ip_requests) > 4096:
            for k in [k for k, v in _ip_requests.items() if not any(t > cutoff for t in v)]:
                _ip_requests.pop(k, None)
        return len(seen) > config.PASSWORD_RESET_IP_MAX


def _expiry_cutoff() -> str:
    return (datetime.fromisoformat(config.now_stamp())
            - timedelta(days=config.PASSWORD_RESET_TTL_DAYS)).isoformat(timespec="seconds")


def request_reset(username: str, new_password: str, ip: str = "",
                  note: Optional[str] = None) -> None:
    """Record a request. Returns nothing, whatever happened - see module docs.

    Raises ValueError for a password that could never be accepted (that says
    nothing about the account) and ResetThrottled for too many requests.
    """
    auth.validate_password(new_password)
    if _throttled(ip):
        log.warning("Password reset requests throttled for address %s", ip)
        raise ResetThrottled()

    # Hashed whether or not the account exists, so the time taken does not
    # say which usernames are real.
    new_hash = auth.hash_password(new_password)
    user = auth.get_user_by_username((username or "").strip())
    # Only accounts that could sign in with a password. A pending or rejected
    # registration is not locked out by a forgotten password, and approving a
    # password for one would look like approving the account.
    if not user or (user.get("status") or "active") != "active":
        log.info("Password reset requested for a username with no active account")
        return

    now = config.now_stamp()
    note = (note or "").strip()[:200] or None
    with database.connect() as conn:
        # One live request per account: the newest is the one the person
        # remembers making, so any earlier one stops being approvable.
        conn.execute(
            "UPDATE password_reset_requests SET status = 'superseded', decided_at = ?, "
            "new_password_hash = '' WHERE user_id = ? AND status = 'pending'",
            (now, user["id"]),
        )
        conn.execute(
            "INSERT INTO password_reset_requests "
            "(user_id, new_password_hash, status, note, requested_at, requested_ip) "
            "VALUES (?,?,?,?,?,?)",
            (user["id"], new_hash, "pending", note, now, ip or None),
        )
    log.info("Password reset requested for user %s", user["id"])


def _expire_old(conn) -> None:
    conn.execute(
        "UPDATE password_reset_requests SET status = 'expired', new_password_hash = '' "
        "WHERE status = 'pending' AND requested_at < ?",
        (_expiry_cutoff(),),
    )


def list_pending() -> List[dict]:
    """Waiting requests, newest first, with who they are for. No hashes."""
    with database.connect() as conn:
        _expire_old(conn)
        rows = conn.execute(
            "SELECT r.id, r.user_id, r.note, r.requested_at, r.requested_ip, "
            "       u.username, u.full_name, u.role, u.is_active, u.phone, u.email, "
            "       c.name AS centre_name, "
            # How many requests this account has had lately - several from a
            # stranger's addresses is a reason to be suspicious.
            "       (SELECT COUNT(*) FROM password_reset_requests r2 "
            "         WHERE r2.user_id = r.user_id AND r2.requested_at >= ?) AS recent_requests "
            "FROM password_reset_requests r "
            "JOIN users u ON u.id = r.user_id "
            "LEFT JOIN centres c ON c.id = u.centre_id "
            "WHERE r.status = 'pending' "
            "ORDER BY r.requested_at DESC, r.id DESC",
            (_expiry_cutoff(),),
        ).fetchall()
    return [dict(r) for r in rows]


def pending_count() -> int:
    with database.connect() as conn:
        _expire_old(conn)
        return int(conn.execute(
            "SELECT COUNT(*) FROM password_reset_requests WHERE status = 'pending'"
        ).fetchone()[0])


def decide(request_id: int, approve: bool, admin_id: int) -> dict:
    """Approve or reject one waiting request. Returns {user_id, username}."""
    now = config.now_stamp()
    with database.connect() as conn:
        _expire_old(conn)
        row = conn.execute(
            "SELECT r.id, r.user_id, r.status, r.new_password_hash, u.username "
            "FROM password_reset_requests r JOIN users u ON u.id = r.user_id "
            "WHERE r.id = ? FOR UPDATE OF r",
            (request_id,),
        ).fetchone()
        if not row:
            raise ResetNotFound()
        if row["status"] != "pending":
            raise ResetNotPending(row["status"])
        if approve:
            conn.execute(
                "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL "
                "WHERE id = ?",
                (row["new_password_hash"], row["user_id"]),
            )
            # Signed out everywhere, as with any password change.
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (row["user_id"],))
        # The hash has no use once decided - approved, it now lives on the
        # account - so it is not kept lying around in a second table.
        conn.execute(
            "UPDATE password_reset_requests SET status = ?, decided_by = ?, decided_at = ?, "
            "new_password_hash = '' WHERE id = ?",
            ("approved" if approve else "rejected", admin_id, now, row["id"]),
        )
    log.info("Password reset %s for user %s %s by admin %s", request_id, row["user_id"],
             "approved" if approve else "rejected", admin_id)
    return {"user_id": int(row["user_id"]), "username": row["username"]}
