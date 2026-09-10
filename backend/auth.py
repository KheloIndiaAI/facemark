"""Authentication and role-based access control.

Two roles, deliberately different in what they can see:

  super_admin - every centre, every athlete and coach, user management
                and centre management.
  coach       - exactly one centre. Every query a coach makes is narrowed to
                their `centre_id`, enforced server-side rather than by hiding
                buttons in the UI.

Passwords are PBKDF2-HMAC-SHA256 (600k iterations, per-user random salt) via
hashlib, so there is no new dependency and no plaintext ever reaches the disk.
Sessions are opaque random tokens stored server-side, which means logout and
deactivation take effect immediately - a self-contained JWT could not be
revoked before it expired.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request

from . import config, database

log = logging.getLogger("auth")

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_HOURS = 12
TOKEN_BYTES = 32


# --- password hashing --------------------------------------------------------

MIN_PASSWORD_LEN = 6


def validate_password(password: str) -> None:
    """Raise if this password is unacceptable. Cheap - no hashing.

    Split out of hash_password so a caller can check BEFORE doing anything
    expensive or anything it would have to undo. Self-registration learned the
    password was too short only after committing the person row.
    """
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")


def hash_password(password: str) -> str:
    """-> 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'."""
    validate_password(password)
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification; False on any malformed record."""
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# --- users -------------------------------------------------------------------

def create_user(
    username: str,
    password: str,
    role: str,
    full_name: str,
    centre_id: Optional[int] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    student_id: Optional[int] = None,
    status: str = "active",
) -> int:
    if role not in ("super_admin", "coach", "athlete"):
        raise ValueError(f"Unknown role: {role}")
    if role == "coach" and centre_id is None:
        raise ValueError("A coach must be assigned to a centre")
    if role == "athlete" and student_id is None:
        # An athlete account with no person behind it could never mark itself
        # present - there would be no templates to verify against and no roster
        # entry to draft. Refuse at creation rather than at first use.
        raise ValueError("An athlete account must be linked to an enrolled person")
    # Display-only stamp, never compared - so it uses the centre's clock like
    # every other stored timestamp. The session expiry below deliberately does
    # NOT: those datetime.now() calls are only ever compared against each
    # other, and moving one side of that comparison is how sessions end up
    # never expiring.
    now = config.now_stamp()
    with database.connect() as conn:
        # `status` is written by the INSERT rather than by a follow-up UPDATE.
        # Self-registration used to create the row (default 'active'), then mark
        # it pending in a second transaction - and in between, an account nobody
        # had approved could sign in.
        return conn.insert(
            "INSERT INTO users (username, password_hash, role, full_name, email, phone, "
            "centre_id, student_id, is_active, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (
                username.strip().lower(), hash_password(password), role, full_name.strip(),
                email, phone, centre_id, student_id, now, status,
            ),
        )


def get_user_by_username(username: str) -> Optional[dict]:
    """Join the centre so the login response carries centre_name, matching what
    /auth/me returns. Without the join a coach's session started life labelled
    'unassigned' until the next page load."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT u.*, c.name AS centre_name FROM users u "
            "LEFT JOIN centres c ON c.id = u.centre_id WHERE u.username = ?",
            (username.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None


def list_users(centre_id: Optional[int] = None) -> list:
    q = (
        "SELECT u.id, u.username, u.role, u.full_name, u.email, u.phone, u.centre_id, "
        # status was missing, so the Accounts page could not tell an approved
        # account from a rejected one - the only screen a super admin has for
        # looking at accounts showed nothing about the decision made on them.
        "u.is_active, u.status, u.approved_at, u.last_login, u.created_at, "
        "c.name AS centre_name "
        "FROM users u LEFT JOIN centres c ON c.id = u.centre_id"
    )
    params = []
    if centre_id is not None:
        q += " WHERE u.centre_id = ?"
        params.append(centre_id)
    # Newest first. Sorting by role then name scattered the accounts somebody
    # actually has to act on - a registration that just arrived - through an
    # alphabetical list. id descending is the creation order without needing
    # created_at to be parseable.
    q += " ORDER BY u.id DESC"
    with database.connect() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def set_user_active(user_id: int, active: bool) -> None:
    with database.connect() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(active), user_id))
        if not active:  # revoke live sessions immediately
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def change_password(user_id: int, new_password: str) -> None:
    with database.connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def delete_user(user_id: int) -> None:
    with database.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --- sessions ----------------------------------------------------------------

class AccountNotActive(Exception):
    """Pending, suspended or rejected. NOT a throttle.

    Separate from LoginBlocked because the two need different answers: a
    throttled caller should wait and retry, and this caller should not - no
    amount of waiting approves an account. Reusing 429 here told clients to
    back off and try again forever.
    """

    def __init__(self, message: str, status: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status


class LoginBlocked(Exception):
    """Raised instead of returning None, so the caller can say WHY."""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


# Per-address attempt times. In-process, so N workers allow N times the burst -
# accepted deliberately, because the alternative is letting an unauthenticated
# caller write a row per attempt.
_ip_attempts: dict = {}
_ip_lock = threading.Lock()


def _ip_throttled(ip: str) -> bool:
    """True when this address has tried too many times recently."""
    if not ip:
        return False
    now = time.time()
    cutoff = now - config.LOGIN_IP_WINDOW_SECONDS
    with _ip_lock:
        seen = [t for t in _ip_attempts.get(ip, ()) if t > cutoff]
        seen.append(now)
        _ip_attempts[ip] = seen
        # Drop addresses that have gone quiet, or the dict grows without bound.
        if len(_ip_attempts) > 4096:
            for k in [k for k, v in _ip_attempts.items() if not any(t > cutoff for t in v)]:
                _ip_attempts.pop(k, None)
        return len(seen) > config.LOGIN_IP_MAX_ATTEMPTS


def _note_failure(user_id: int, failures: int) -> None:      # noqa: ARG001
    """Count one failed attempt, atomically.

    `failures` is accepted only for call compatibility and is deliberately
    IGNORED: it was read before verify_password spent 600,000 PBKDF2 rounds, so
    by the time it arrived here it was stale by hundreds of milliseconds. Every
    concurrent guess read the same value and wrote the same value back, which
    turned a burst of N attempts into a single increment - and a slow request
    carrying an old count could overwrite a lock a faster one had just set,
    because the UPDATE wrote locked_until unconditionally too.

    The database does the arithmetic now, in one statement, so N concurrent
    attempts cost N.
    """
    locked_at = (datetime.now() + timedelta(seconds=config.LOGIN_LOCKOUT_SECONDS)) \
        .isoformat(timespec="seconds")
    with database.connect() as conn:
        row = conn.execute(
            "UPDATE users SET failed_attempts = COALESCE(failed_attempts, 0) + 1, "
            "  locked_until = CASE "
            "    WHEN COALESCE(failed_attempts, 0) + 1 >= ? THEN ? "
            "    ELSE locked_until END "
            " WHERE id = ? RETURNING failed_attempts, locked_until",
            (config.LOGIN_MAX_FAILURES, locked_at, user_id),
        ).fetchone()
    if row and row["locked_until"]:
        log.warning("Account %s locked after %d failed attempts",
                    user_id, row["failed_attempts"])


def login(username: str, password: str, ip: str = "") -> Optional[dict]:
    """Verify credentials and open a session. Returns {token, user} or None.

    Raises LoginBlocked when throttled. Both throttle checks run BEFORE the
    password is hashed: verification is 600,000 PBKDF2 rounds, so a guard that
    hashed first would still burn the CPU it exists to protect.
    """
    if _ip_throttled(ip):
        log.warning("Login throttled for address %s", ip)
        raise LoginBlocked(
            "Too many sign-in attempts from this device. Wait a few minutes.",
            config.LOGIN_IP_WINDOW_SECONDS,
        )

    user = get_user_by_username(username)
    if not user or not user["is_active"]:
        return None

    locked_until = user.get("locked_until")
    if locked_until:
        try:
            until = datetime.fromisoformat(locked_until)
        except (TypeError, ValueError):
            until = None
        if until and until > datetime.now():
            raise LoginBlocked(
                "This account is temporarily locked after too many failed "
                "attempts. Try again later, or ask an admin to reset it.",
                int((until - datetime.now()).total_seconds()),
            )

    # Checked BEFORE the password comparison so a pending account cannot be
    # probed for a valid password, and so the 600k-round hash is not spent on
    # an account that cannot sign in either way.
    status = (user.get("status") or "active")
    if status != "active":
        raise AccountNotActive(
            {
                "pending": "This account is waiting to be approved. "
                           "You will be able to sign in once that happens.",
                # Named, because "not active" sent people to wait for something
                # that was never going to arrive. A rejection can be reopened,
                # so this says who to ask rather than closing the door.
                "rejected": "This registration was not approved. Ask your coach "
                            "or an administrator if you think that is a mistake.",
            }.get(status, "This account is not active. Ask an administrator."),
            status,
        )
    if not verify_password(password, user["password_hash"]):
        _note_failure(int(user["id"]), int(user.get("failed_attempts") or 0))
        return None

    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = datetime.now()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
            (
                token, user["id"],
                (now + timedelta(hours=SESSION_TTL_HOURS)).isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        # A success clears the throttle: the lock is there to slow an attacker,
        # not to punish someone who mistyped twice before getting it right.
        conn.execute(
            "UPDATE users SET last_login = ?, failed_attempts = 0, locked_until = NULL "
            "WHERE id = ?",
            # The CENTRE's clock, not the server's. `now` here is
            # datetime.now(), which is correct for the session expiry above -
            # those values are only ever compared against each other, and
            # moving one side is how sessions stop expiring. last_login is
            # different: it is DISPLAYED, on the Accounts page, next to
            # created_at and approved_at, which config.now_stamp() writes on the
            # centre's clock. On a UTC server that put one column five and a
            # half hours behind the ones beside it, and an administrator reading
            # "last sign-in 03:40" for somebody who signed in at 09:10 has no
            # way to tell which column is lying.
            (config.now_stamp(), user["id"]),
        )
        conn.execute(
            "DELETE FROM auth_sessions WHERE expires_at < ?",
            (now.isoformat(timespec="seconds"),),
        )
    return {"token": token, "user": public_user(user)}


def logout(token: str) -> None:
    with database.connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


def resolve_token(token: str) -> Optional[dict]:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at, c.name AS centre_name FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id "
            "LEFT JOIN centres c ON c.id = u.centre_id "
            "WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        logout(token)
        return None
    if not row["is_active"]:
        return None
    return dict(row)


def public_user(user: dict) -> dict:
    """The user object safe to send to the browser (no hash, no salt)."""
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "full_name": user["full_name"],
        "email": user.get("email"),
        "centre_id": user.get("centre_id"),
        "centre_name": user.get("centre_name"),
        "is_super_admin": user["role"] == "super_admin",
    }


# --- FastAPI dependencies ----------------------------------------------------

def _token_from_request(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get("facemark_token")


def current_user(request: Request) -> dict:
    """Require any authenticated user."""
    token = _token_from_request(request)
    if not token:
        raise HTTPException(401, "Sign in to continue")
    user = resolve_token(token)
    if not user:
        raise HTTPException(401, "Session expired - sign in again")
    return user


def client_ip(request) -> str:
    """The address a throttle may safely be keyed on.

    X-FORWARDED-FOR IS WRITTEN BY THE CLIENT. Both throttles used to take its
    LEFTMOST value, and nothing in this application established a trusted proxy,
    so an anonymous caller sending a different value on every request got a
    fresh bucket every time. That defeated the login lockout and, worse, the
    signup limit - and /api/signup is unauthenticated and runs 600,000 PBKDF2
    rounds per call, which makes an unbounded one a way to saturate both workers
    from a laptop as well as to flood every coach's approval queue.

    A proxy APPENDS the peer address to the chain, so with N trusted proxies in
    front the real client is the Nth entry from the RIGHT. Anything further left
    was supplied by the caller and is not evidence of anything.

    TRUSTED_PROXY_HOPS defaults to 0: no proxy, so the header is ignored
    entirely and the socket address is used. Deployments behind Caddy or an ALB
    set it to the number of hops they actually have. Getting it wrong in the
    cautious direction costs a shared bucket; getting it wrong the other way is
    what this fixes.
    """
    peer = ""
    try:
        peer = request.client.host if request.client else ""
    except Exception:      # noqa: BLE001 - a missing client is not an error here
        peer = ""
    hops = config.TRUSTED_PROXY_HOPS
    if hops <= 0:
        return peer
    chain = [p.strip() for p in
             (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    if not chain:
        return peer
    # The peer itself is the last hop and is not in the header, so N hops means
    # the client sits N-1 from the right of what the header does contain.
    idx = len(chain) - hops
    if idx < 0:
        # Fewer entries than hops claimed: the chain is shorter than configured,
        # so nothing in it is trustworthy. Fall back to the socket.
        return peer
    return chain[idx] or peer


def assert_super_admin(user: dict) -> dict:
    """The same rule as require_super_admin, callable as a plain function.

    A FastAPI dependency only runs when FastAPI dispatches the request. One
    route called another route's function DIRECTLY - process_attendance_video
    calling process_attendance - and the Depends(require_super_admin) on the
    callee simply did not execute, handing every coach the confirmed-attendance
    writer that guard exists to withhold. A route that may be called in-process
    needs a check that is a statement, not a signature.
    """
    if user.get("role") != "super_admin":
        raise HTTPException(403, "This action is restricted to super admins")
    return user


def require_super_admin(user: dict = Depends(current_user)) -> dict:
    return assert_super_admin(user)


def scope_centre(user: dict, requested: Optional[int] = None) -> Optional[int]:
    """The centre id a request may actually read.

    A coach is pinned to their own centre no matter what the client asks for -
    passing someone else's centre_id in the query string must not widen access.
    A super admin gets whatever they asked for, or None meaning "all centres".
    """
    if user["role"] == "super_admin":
        return requested
    # A coach with no centre used to fall through and return None, and None
    # downstream means "every centre" - so losing a centre (it was deleted, or
    # the account was created without one) silently widened access instead of
    # removing it. A coach without a centre has no scope at all.
    if user.get("centre_id") is None:
        raise HTTPException(
            403, "This account is not assigned to a centre, so it cannot read "
                 "centre data. An administrator needs to assign one.")
    if requested is not None and requested != user["centre_id"]:
        raise HTTPException(403, "You can only access your own centre")
    return user["centre_id"]


def owns_centre(user: dict, centre_id: Optional[int]) -> None:
    """Refuse unless this caller's centre owns the thing being acted on.

    scope_centre answers "which centre may this request READ", and a None from
    the caller legitimately means "no filter asked for". Passing a PERSON's
    centre into it therefore reads the wrong way round: a person whose
    centre_id is NULL produced requested=None, which is the "no filter" case,
    and the check silently passed - so any coach could act on any centre-less
    person, including deleting them.

    This is the other question, and it has no permissive case.
    """
    if user.get("role") == "super_admin":
        return
    if centre_id is None or user.get("centre_id") is None or \
            int(centre_id) != int(user["centre_id"]):
        raise HTTPException(403, "That person is not at your centre")


def coach_student_id(user: dict) -> int:
    """The students row this coach IS, not the athletes they coach.

    A coach is a person in `students` as well as an account in `users`, because
    they have a face and get recognised like anyone else. Sessions and
    coach_athletes both key on that students id, so an account without one
    cannot open a register - and that is a configuration error worth naming
    rather than a foreign-key violation deep in an insert.
    """
    sid = user.get("student_id")
    if not sid:
        raise HTTPException(
            400,
            "This account is not linked to a person record, so it cannot take "
            "attendance. A super admin needs to link it to an enrolled coach.",
        )
    return int(sid)


def scope_coach(user: dict, coach_id: Optional[int]) -> Optional[int]:
    """The coach id this request may act as.

    A coach may only ever act on their own register - passing someone else's
    coach_id must not widen access, the same way scope_centre refuses a
    different centre. A super admin may act as any coach, or as None for a
    centre-wide sweep.
    """
    if user["role"] == "super_admin":
        return coach_id
    own = coach_student_id(user)
    if coach_id is not None and int(coach_id) != own:
        raise HTTPException(403, "You can only work on your own register")
    return own


def scope_self(user: dict, student_id: Optional[int]) -> int:
    """The person this request may read or write attendance for.

    An athlete is NOT a coach with fewer buttons: they may only ever reach
    their own record. A super admin may act for anyone; a coach may not use
    this path at all, because a coach acting for an athlete goes through the
    register, where it is reviewed and signed for.
    """
    if user["role"] == "super_admin":
        if student_id is None:
            raise HTTPException(400, "A person is required")
        return int(student_id)
    own = user.get("student_id")
    if not own:
        raise HTTPException(400, "This account is not linked to a person record")
    if student_id is not None and int(student_id) != int(own):
        raise HTTPException(403, "You can only see your own attendance")
    return int(own)


def require_staff(user: dict = Depends(current_user)) -> dict:
    """Coach or super admin - anybody who runs a centre rather than attends it.

    The gap this closes: several routes took `current_user` and then scoped by
    centre, which reads like access control but is not. An athlete HAS a centre,
    so a centre scope let them through to writes meant for the person taking the
    register.
    """
    if user["role"] not in ("coach", "super_admin"):
        raise HTTPException(403, "This action is for coaches and administrators")
    return user


def require_athlete(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ("athlete", "super_admin"):
        raise HTTPException(403, "This is for athlete accounts")
    return user


def bootstrap_default_admin() -> Optional[str]:
    """Create the first super admin if no users exist yet.

    Returns the password ONLY when this function invented it, because then there
    is no other way for the operator to learn it. A password supplied through
    FACEMARK_ADMIN_PASSWORD is already known to whoever set it, and echoing it
    into the application log - durable on this deployment, and readable by
    anyone with log access long after the console has scrolled away - published
    a production credential for no benefit at all.
    """
    with database.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return None
    supplied = os.environ.get("FACEMARK_ADMIN_PASSWORD")
    password = supplied or secrets.token_urlsafe(9)
    create_user(
        username="admin",
        password=password,
        role="super_admin",
        full_name="System Administrator",
    )
    if supplied:
        log.info("First run: super admin 'admin' created with the password "
                 "supplied in FACEMARK_ADMIN_PASSWORD (not logged).")
        return None
    return password
